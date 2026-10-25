"""s2 carrier bookings, in process against a real MongoDB (own database, dropped at the end).

Covers services/carrier_booking.py and the partner accept hook in routes/partner.py: sandbox bookings confirm once and
update the leg, e-mail (log) bookings stay requested until confirmed, rebooking cancels and moves the leg, ranking
(rate, lane card validity, reliability, DG acceptance, quoted-carrier preference) and the DCSA Booking 2.0 request.

The DCSA schema and responses below are not written from memory: DCSA_CREATE_BOOKING_DEFS is the CreateBooking part of the
published OpenAPI document https://api.swaggerhub.com/apis/dcsaorg/DCSA_BKG/2.0.0 (fetched 2026-10-24; annotations
dropped, OpenAPI 3.0 boolean exclusiveMinimum turned into the JSON Schema form, additionalProperties=false added so a
misspelt field fails). All four published request examples (dry, reefer, NOR, DG) validate against it. The HTTP doubles
answer with that document's own examples (POST 202 acceptedExample, GET 200 dryConfirmedExample, trimmed).
"""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_carriers_{uuid.uuid4().hex[:8]}"
os.environ.setdefault("EMAIL_PROVIDER", "log")
os.environ["DIGITRAFFIC_ENABLED"] = "false"
os.environ.pop("CARRIER_BOOKING_MODE", None)
import asyncio  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
from datetime import timedelta  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


import httpx  # noqa: E402
import jsonschema  # noqa: E402
import pytest  # noqa: E402

DCSA_CREATE_BOOKING_DEFS = {'ActiveReeferSettings': {'additionalProperties': False,
                              'properties': {'airExchangeSetpoint': {'minimum': 0, 'type': 'number'},
                                             'airExchangeUnit': {'enum': ['MQH', 'FQH'], 'type': 'string'},
                                             'co2Setpoint': {'maximum': 100, 'minimum': 0, 'type': 'number'},
                                             'humiditySetpoint': {'maximum': 100, 'minimum': 0, 'type': 'number'},
                                             'isBulbMode': {'type': 'boolean'},
                                             'isColdTreatmentRequired': {'type': 'boolean'},
                                             'isControlledAtmosphereRequired': {'type': 'boolean'},
                                             'isDrainholesOpen': {'type': 'boolean'},
                                             'isGeneratorSetRequired': {'type': 'boolean'},
                                             'isPreCoolingRequired': {'type': 'boolean'},
                                             'isVentilationOpen': {'type': 'boolean'},
                                             'o2Setpoint': {'maximum': 100, 'minimum': 0, 'type': 'number'},
                                             'temperatureSetpoint': {'type': 'number'},
                                             'temperatureUnit': {'enum': ['CEL', 'FAH'], 'type': 'string'}},
                              'required': ['temperatureSetpoint', 'temperatureUnit'],
                              'type': 'object'},
     'BookingAgent': {'additionalProperties': False,
                      'properties': {'address': {'$ref': '#/$defs/PartyAddress'},
                                     'identifyingCodes': {'items': {}, 'type': 'array'},
                                     'partyContactDetails': {'items': {'$ref': '#/$defs/PartyContactDetail'}, 'minItems': 1, 'type': 'array'},
                                     'partyName': {'maxLength': 70, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                     'reference': {'maxLength': 35, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                     'taxLegalReferences': {'items': {}, 'type': 'array'}},
                      'required': ['partyName'],
                      'type': 'object'},
     'CargoGrossVolume': {'additionalProperties': False,
                          'properties': {'unit': {'enum': ['MTQ', 'FTQ'], 'type': 'string'},
                                         'value': {'exclusiveMinimum': 0, 'type': 'number'}},
                          'required': ['value', 'unit'],
                          'type': 'object'},
     'CargoGrossWeight': {'additionalProperties': False,
                          'properties': {'unit': {'enum': ['KGM', 'LBR'], 'type': 'string'},
                                         'value': {'exclusiveMinimum': 0, 'type': 'number'}},
                          'required': ['value', 'unit'],
                          'type': 'object'},
     'CargoGrossWeightReq': {'additionalProperties': False,
                             'properties': {'unit': {'enum': ['KGM', 'LBR'], 'type': 'string'},
                                            'value': {'exclusiveMinimum': 0, 'type': 'number'}},
                             'required': ['value', 'unit'],
                             'type': 'object'},
     'CommodityShipper': {'additionalProperties': False,
                          'properties': {'HSCodes': {'items': {'maxLength': 10, 'minLength': 6, 'pattern': '^\\d{6,10}$', 'type': 'string'},
                                                     'type': 'array'},
                                         'cargoGrossVolume': {'$ref': '#/$defs/CargoGrossVolume'},
                                         'cargoGrossWeight': {'$ref': '#/$defs/CargoGrossWeight'},
                                         'cargoNetVolume': {},
                                         'cargoNetWeight': {},
                                         'commodityType': {'maxLength': 550, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                         'customsReferences': {'items': {}, 'type': 'array'},
                                         'exportLicense': {},
                                         'importLicense': {},
                                         'nationalCommodityCodes': {'items': {}, 'type': 'array'},
                                         'outerPackaging': {'$ref': '#/$defs/OuterPackaging'},
                                         'references': {'items': {'$ref': '#/$defs/ReferenceShipper'}, 'type': 'array'}},
                          'required': ['commodityType'],
                          'type': 'object'},
     'Consignee': {'additionalProperties': False,
                   'properties': {'address': {'$ref': '#/$defs/PartyAddress'},
                                  'identifyingCodes': {'items': {}, 'type': 'array'},
                                  'partyContactDetails': {'items': {'$ref': '#/$defs/PartyContactDetail'}, 'minItems': 1, 'type': 'array'},
                                  'partyName': {'maxLength': 70, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                  'purchaseOrderReferences': {'items': {'maxLength': 35, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                                              'type': 'array'},
                                  'reference': {'maxLength': 35, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                  'taxLegalReferences': {'items': {}, 'type': 'array'}},
                   'required': ['partyName'],
                   'type': 'object'},
     'CreateBooking': {'additionalProperties': False,
                       'properties': {'bookingChannelReference': {'maxLength': 20, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                      'cargoMovementTypeAtDestination': {'maxLength': 3, 'type': 'string'},
                                      'cargoMovementTypeAtOrigin': {'maxLength': 3, 'type': 'string'},
                                      'carrierCode': {'maxLength': 4, 'pattern': '^\\S+$', 'type': 'string'},
                                      'carrierCodeListProvider': {'enum': ['SMDG', 'NMFTA'], 'type': 'string'},
                                      'carrierExportVoyageNumber': {'maxLength': 50, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                      'carrierServiceCode': {'maxLength': 11, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                      'carrierServiceName': {'maxLength': 50, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                      'contractQuotationReference': {'maxLength': 35, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                      'customsReferences': {'items': {}, 'type': 'array'},
                                      'declaredValue': {'minimum': 0, 'type': 'number'},
                                      'declaredValueCurrency': {'maxLength': 3, 'minLength': 3, 'pattern': '^[A-Z]{3}$', 'type': 'string'},
                                      'deliveryTypeAtDestination': {'enum': ['CY', 'SD', 'CFS'], 'maxLength': 3, 'type': 'string'},
                                      'destinationChargesPaymentTerm': {},
                                      'documentParties': {'$ref': '#/$defs/DocumentPartiesReq'},
                                      'expectedArrivalAtPlaceOfDeliveryEndDate': {'type': 'string'},
                                      'expectedArrivalAtPlaceOfDeliveryStartDate': {'type': 'string'},
                                      'expectedDepartureDate': {'type': 'string'},
                                      'exportDeclarationReference': {'maxLength': 35, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                      'freightPaymentTermCode': {'enum': ['PRE', 'COL'], 'type': 'string'},
                                      'incoTerms': {'maxLength': 3, 'type': 'string'},
                                      'invoicePayableAt': {'$ref': '#/$defs/InvoicePayableAt'},
                                      'isEquipmentSubstitutionAllowed': {'type': 'boolean'},
                                      'isExportDeclarationRequired': {'type': 'boolean'},
                                      'isPartialLoadAllowed': {'type': 'boolean'},
                                      'originChargesPaymentTerm': {},
                                      'partyContactDetails': {'items': {'$ref': '#/$defs/PartyContactDetail'}, 'type': 'array'},
                                      'placeOfBLIssue': {},
                                      'receiptTypeAtOrigin': {'enum': ['CY', 'SD', 'CFS'], 'maxLength': 3, 'type': 'string'},
                                      'references': {'items': {'$ref': '#/$defs/ReferenceShipper'}, 'type': 'array'},
                                      'requestedEquipments': {'items': {'$ref': '#/$defs/RequestedEquipmentShipper'},
                                                              'minItems': 1,
                                                              'type': 'array'},
                                      'serviceContractReference': {'maxLength': 30, 'type': 'string'},
                                      'shipmentLocations': {'items': {'$ref': '#/$defs/ShipmentLocation'}, 'minItems': 1, 'type': 'array'},
                                      'transportDocumentReference': {'maxLength': 20, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                      'transportDocumentTypeCode': {'enum': ['BOL', 'SWB'], 'type': 'string'},
                                      'universalExportVoyageReference': {'maxLength': 5,
                                                                         'minLength': 5,
                                                                         'pattern': '^\\d{2}[0-9A-Z]{2}[NEWSR]$',
                                                                         'type': 'string'},
                                      'universalServiceReference': {'maxLength': 8,
                                                                    'minLength': 8,
                                                                    'pattern': '^SR\\d{5}[A-Z]$',
                                                                    'type': 'string'},
                                      'vessel': {'$ref': '#/$defs/Vessel'}},
                       'required': ['receiptTypeAtOrigin', 'deliveryTypeAtDestination', 'cargoMovementTypeAtOrigin',
                                    'cargoMovementTypeAtDestination', 'isEquipmentSubstitutionAllowed', 'shipmentLocations',
                                    'requestedEquipments', 'documentParties'],
                       'type': 'object'},
     'DangerousGoods': {'oneOf': [{'properties': {'UNNumber': {'maxLength': 4, 'minLength': 4, 'pattern': '^\\d{4}$', 'type': 'string'}},
                                   'required': ['UNNumber'],
                                   'type': 'object'},
                                  {'properties': {'NANumber': {'maxLength': 4, 'minLength': 4, 'pattern': '^\\d{4}$', 'type': 'string'}},
                                   'required': ['NANumber'],
                                   'type': 'object'}],
                        'properties': {'EMSNumber': {'maxLength': 7, 'type': 'string'},
                                       'additionalContainerCargoHandling': {'maxLength': 255, 'type': 'string'},
                                       'codedVariantList': {'maxLength': 4, 'minLength': 4, 'pattern': '^[0-3][0-9A-Z]{3}$', 'type': 'string'},
                                       'competentAuthorityApproval': {'maxLength': 70, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                       'emergencyContactDetails': {'$ref': '#/$defs/EmergencyContactDetails'},
                                       'endOfHoldingTime': {'type': 'string'},
                                       'fumigationDateTime': {'type': 'string'},
                                       'grossWeight': {'$ref': '#/$defs/GrossWeight'},
                                       'imoClass': {'maxLength': 4, 'type': 'string'},
                                       'inhalationZone': {'maxLength': 1, 'minLength': 1, 'type': 'string'},
                                       'innerPackagings': {'items': {}, 'type': 'array'},
                                       'isCompetentAuthorityApprovalRequired': {'type': 'boolean'},
                                       'isEmptyUncleanedResidue': {'type': 'boolean'},
                                       'isExceptedQuantity': {'type': 'boolean'},
                                       'isHot': {'type': 'boolean'},
                                       'isLimitedQuantity': {'type': 'boolean'},
                                       'isMarinePollutant': {'type': 'boolean'},
                                       'isReportableQuantity': {'type': 'boolean'},
                                       'isSalvagePackings': {'type': 'boolean'},
                                       'isWaste': {'type': 'boolean'},
                                       'limits': {},
                                       'netExplosiveContent': {},
                                       'netVolume': {},
                                       'netWeight': {},
                                       'packingGroup': {'maximum': 3, 'minimum': 1, 'type': 'integer'},
                                       'properShippingName': {'maxLength': 250, 'type': 'string'},
                                       'segregationGroups': {'items': {'maxLength': 2, 'type': 'string'}, 'type': 'array'},
                                       'specialCertificateNumber': {'maxLength': 255, 'type': 'string'},
                                       'subsidiaryRisk1': {'maxLength': 3, 'minLength': 1, 'pattern': '^[0-9](\\.[0-9])?$', 'type': 'string'},
                                       'subsidiaryRisk2': {'maxLength': 3, 'minLength': 1, 'pattern': '^[0-9](\\.[0-9])?$', 'type': 'string'},
                                       'technicalName': {'maxLength': 250, 'type': 'string'}},
                        'propertyNames': {'enum': ['EMSNumber', 'NANumber', 'UNNumber', 'additionalContainerCargoHandling', 'codedVariantList',
                                                   'competentAuthorityApproval', 'emergencyContactDetails', 'endOfHoldingTime',
                                                   'fumigationDateTime', 'grossWeight', 'imoClass', 'inhalationZone', 'innerPackagings',
                                                   'isCompetentAuthorityApprovalRequired', 'isEmptyUncleanedResidue', 'isExceptedQuantity',
                                                   'isHot', 'isLimitedQuantity', 'isMarinePollutant', 'isReportableQuantity',
                                                   'isSalvagePackings', 'isWaste', 'limits', 'netExplosiveContent', 'netVolume', 'netWeight',
                                                   'packingGroup', 'properShippingName', 'segregationGroups', 'specialCertificateNumber',
                                                   'subsidiaryRisk1', 'subsidiaryRisk2', 'technicalName']},
                        'required': ['properShippingName', 'imoClass', 'isMarinePollutant', 'isLimitedQuantity', 'isExceptedQuantity',
                                     'isSalvagePackings', 'isEmptyUncleanedResidue', 'isWaste', 'isHot', 'isCompetentAuthorityApprovalRequired',
                                     'isReportableQuantity', 'emergencyContactDetails', 'grossWeight'],
                        'type': 'object'},
     'DocumentPartiesReq': {'additionalProperties': False,
                            'properties': {'bookingAgent': {'$ref': '#/$defs/BookingAgent'},
                                           'carrierBookingOffice': {},
                                           'consignee': {'$ref': '#/$defs/Consignee'},
                                           'other': {'items': {}, 'type': 'array'},
                                           'serviceContractOwner': {},
                                           'shipper': {'$ref': '#/$defs/Shipper'}},
                            'required': ['bookingAgent'],
                            'type': 'object'},
     'EmergencyContactDetails': {'additionalProperties': False,
                                 'properties': {'contact': {'maxLength': 255, 'type': 'string'},
                                                'phone': {'maxLength': 30, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                                'provider': {'maxLength': 255, 'type': 'string'},
                                                'referenceNumber': {'maxLength': 255, 'type': 'string'}},
                                 'required': ['contact', 'phone'],
                                 'type': 'object'},
     'GrossWeight': {'additionalProperties': False,
                     'properties': {'unit': {'enum': ['KGM', 'LBR'], 'type': 'string'}, 'value': {'exclusiveMinimum': 0, 'type': 'number'}},
                     'required': ['value', 'unit'],
                     'type': 'object'},
     'InvoicePayableAt': {'additionalProperties': False,
                          'properties': {'UNLocationCode': {'maxLength': 5,
                                                            'minLength': 5,
                                                            'pattern': '^[A-Z]{2}[A-Z2-9]{3}$',
                                                            'type': 'string'}},
                          'required': ['UNLocationCode'],
                          'type': 'object'},
     'Location': {'additionalProperties': False,
                  'properties': {'UNLocationCode': {'maxLength': 5, 'minLength': 5, 'pattern': '^[A-Z]{2}[A-Z2-9]{3}$', 'type': 'string'},
                                 'address': {},
                                 'facility': {},
                                 'geoCoordinate': {},
                                 'locationName': {'maxLength': 100, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'}},
                  'type': 'object'},
     'OuterPackaging': {'additionalProperties': False,
                        'properties': {'dangerousGoods': {'items': {'$ref': '#/$defs/DangerousGoods'}, 'type': 'array'},
                                       'description': {'maxLength': 100, 'type': 'string'},
                                       'imoPackagingCode': {'maxLength': 5, 'minLength': 1, 'pattern': '^[A-Z0-9]{1,5}$', 'type': 'string'},
                                       'numberOfPackages': {'maximum': 99999999, 'minimum': 1, 'type': 'integer'},
                                       'packageCode': {'maxLength': 2, 'minLength': 2, 'pattern': '^[A-Z0-9]{2}$', 'type': 'string'}},
                        'type': 'object'},
     'PartyAddress': {'additionalProperties': False,
                      'properties': {'POBox': {'maxLength': 20, 'type': 'string'},
                                     'UNLocationCode': {'maxLength': 5, 'minLength': 5, 'pattern': '^[A-Z]{2}[A-Z2-9]{3}$', 'type': 'string'},
                                     'city': {'maxLength': 35, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                     'countryCode': {'maxLength': 2, 'minLength': 2, 'pattern': '^[A-Z]{2}$', 'type': 'string'},
                                     'floor': {'maxLength': 50, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                     'postCode': {'maxLength': 10, 'type': 'string'},
                                     'stateRegion': {'maxLength': 65, 'type': 'string'},
                                     'street': {'maxLength': 70, 'type': 'string'},
                                     'streetNumber': {'maxLength': 50, 'type': 'string'}},
                      'required': ['street', 'city', 'countryCode'],
                      'type': 'object'},
     'PartyContactDetail': {'anyOf': [{'properties': {'phone': {'maxLength': 30, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'}},
                                       'required': ['phone'],
                                       'type': 'object'},
                                      {'properties': {'email': {'maxLength': 100, 'pattern': '^.+@\\S+$', 'type': 'string'}},
                                       'required': ['email'],
                                       'type': 'object'}],
                            'properties': {'name': {'maxLength': 35, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'}},
                            'propertyNames': {'enum': ['email', 'name', 'phone']},
                            'required': ['name'],
                            'type': 'object'},
     'ReferenceShipper': {'additionalProperties': False,
                          'properties': {'type': {'maxLength': 3, 'type': 'string'},
                                         'value': {'maxLength': 35, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'}},
                          'required': ['type', 'value'],
                          'type': 'object'},
     'RequestedEquipmentShipper': {'additionalProperties': False,
                                   'properties': {'ISOEquipmentCode': {'maxLength': 4, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                                  'activeReeferSettings': {'$ref': '#/$defs/ActiveReeferSettings'},
                                                  'cargoGrossWeight': {'$ref': '#/$defs/CargoGrossWeightReq'},
                                                  'commodities': {'items': {'$ref': '#/$defs/CommodityShipper'}, 'type': 'array'},
                                                  'containerPositionings': {'items': {}, 'type': 'array'},
                                                  'customsReferences': {'items': {}, 'type': 'array'},
                                                  'emptyContainerPickup': {},
                                                  'equipmentReferences': {'items': {'maxLength': 11,
                                                                                    'pattern': '^\\S(?:.*\\S)?$',
                                                                                    'type': 'string'},
                                                                          'type': 'array'},
                                                  'isNonOperatingReefer': {'type': 'boolean'},
                                                  'isShipperOwned': {'type': 'boolean'},
                                                  'references': {'items': {'$ref': '#/$defs/ReferenceShipper'}, 'type': 'array'},
                                                  'tareWeight': {},
                                                  'units': {'minimum': 1, 'type': 'integer'}},
                                   'required': ['ISOEquipmentCode', 'units', 'isShipperOwned'],
                                   'type': 'object'},
     'ShipmentLocation': {'additionalProperties': False,
                          'properties': {'location': {'$ref': '#/$defs/Location'}, 'locationTypeCode': {'maxLength': 3, 'type': 'string'}},
                          'required': ['location', 'locationTypeCode'],
                          'type': 'object'},
     'Shipper': {'additionalProperties': False,
                 'properties': {'address': {'$ref': '#/$defs/PartyAddress'},
                                'identifyingCodes': {'items': {}, 'type': 'array'},
                                'partyContactDetails': {'items': {'$ref': '#/$defs/PartyContactDetail'}, 'minItems': 1, 'type': 'array'},
                                'partyName': {'maxLength': 70, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                'purchaseOrderReferences': {'items': {'maxLength': 35, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                                            'type': 'array'},
                                'reference': {'maxLength': 35, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                                'taxLegalReferences': {'items': {}, 'type': 'array'}},
                 'required': ['partyName'],
                 'type': 'object'},
     'Vessel': {'additionalProperties': False,
                'properties': {'name': {'maxLength': 50, 'pattern': '^\\S(?:.*\\S)?$', 'type': 'string'},
                               'vesselIMONumber': {'maxLength': 8, 'minLength': 7, 'pattern': '^\\d{7,8}$', 'type': 'string'}},
                'required': ['name'],
                'type': 'object'}}

# GET /v2/bookings/{bookingReference} 200, example "dryConfirmedExample" of the same document (fields we read).
DCSA_CONFIRMED_EXAMPLE = {
    "carrierBookingRequestReference": "cbrr-123", "carrierBookingReference": "cbr-987", "bookingStatus": "CONFIRMED",
    "vessel": {"name": "MAERSK IOWA", "vesselIMONumber": "9298686"}, "carrierExportVoyageNumber": "403W",
    "expectedDepartureDate": "2024-01-20", "confirmedEquipments": [{"ISOEquipmentCode": "42G1", "units": 3}],
    "transportPlan": [{"transportPlanStage": "MNC", "transportPlanStageSequenceNumber": 1,
                       "loadLocation": {"locationName": "Bremerhaven", "UNLocationCode": "DEBRV"},
                       "dischargeLocation": {"locationName": "Norfolk", "UNLocationCode": "USORF"},
                       "plannedDepartureDate": "2024-01-20", "plannedArrivalDate": "2024-01-31", "modeOfTransport": "VESSEL",
                       "vesselName": "MAERSK IOWA", "vesselIMONumber": "9298686", "carrierServiceCode": "TA1",
                       "carrierExportVoyageNumber": "403W"}]}
# GET 200 "dryNotConfirmedExample": bookingStatus PENDING_UPDATE (an update is required, still not confirmed)
DCSA_PENDING_EXAMPLE = {"carrierBookingRequestReference": "cbrr-123", "bookingStatus": "PENDING_UPDATE"}

OPS = {"id": "u-ops", "name": "Olli Ops", "email": "olli@huolitsija.test", "org_type": "internal", "role": "ops_admin"}
FIHEL = {"unlocode": "FIHEL", "code": "FIHEL", "name": "Helsinki", "country": "FI", "lat": 60.21, "lon": 25.19}
DEHAM = {"unlocode": "DEHAM", "code": "DEHAM", "name": "Hamburg", "country": "DE", "lat": 53.53, "lon": 9.93}
CARGO = {"load_type": "FCL", "containers": [{"type": "40HC", "count": 1}], "weight_kg": 18000, "packages": 20,
         "package_type": "pallet_eur", "description": "Industrial paint", "dangerous_goods": True,
         "dg": {"un_number": "UN1263", "class": "3", "packing_group": "II", "proper_shipping_name": "PAINT",
                "emergency_phone": "+358 9 123 4567"}}
# code: (name, container cost EUR, on-time %, DG classes, contact e-mail, card valid)
CARRIERS = {
    "ALPHA": ("Alpha Lines", 1500, 80, ["3", "9"], "alpha.booking@carrier.test", True),
    "BRAVO": ("Bravo Shipping", 1200, 90, ["3"], "bravo.booking@carrier.test", True),
    "CHARLIE": ("Charlie Container", 1000, 95, ["3"], "charlie.booking@carrier.test", False),
    "DELTA": ("Delta Feeder", 1100, 92, ["9"], "delta.booking@carrier.test", True),
    "ECHO": ("Echo Lines", 1150, 40, ["3"], "echo.booking@carrier.test", True),
    "GOLF": ("Golf Sea", 3000, 60, ["3"], None, True),
    "HOTEL": ("Hotel Maritime", 2500, 85, ["3"], None, True),
}
W: dict = {}
BOOKED: list = []


async def _seed():
    from core.db import db
    from core.util import now

    today = now().date()
    for code, (name, eur, ot, dg, email, valid) in CARRIERS.items():
        cid = f"car-{code.lower()}"
        W[code] = cid
        await db.raw.carriers.insert_one({"id": cid, "code": code, "name": name, "modes": ["sea"], "country": "FI",
                                          "integration": {"type": "dcsa" if code == "ALPHA" else "manual"}, "active": True,
                                          "on_time_pct": ot, "dg_classes_accepted": dg, "dg_un_blocked": [], "contact_email": email})
        vf, vt = (today - timedelta(days=10), today + timedelta(days=60)) if valid else (today - timedelta(days=90), today - timedelta(days=5))
        await db.raw.rate_cards.insert_one({"id": f"card-{code.lower()}", "carrier_id": cid, "mode": "sea", "service": "FCL",
                                            "basis": "PER_CONTAINER", "origin": {"unlocode": "FIHEL", "radius_km": 40},
                                            "destination": {"unlocode": "DEHAM", "radius_km": 40},
                                            "container_costs": {"40HC": eur * 100}, "surcharges": [], "transit_days": 3,
                                            "valid_from": vf.isoformat(), "valid_to": vt.isoformat(), "source": "contract",
                                            "currency": "EUR", "archived": False})
    W["sandbox_co"] = {"id": "co-sbx", "name": "Sandbox Testi Oy", "business_id": "3990001-4", "test_tenant": True,
                       "kyc_status": "approved", "sanctions_status": "clear"}
    W["normal_co"] = {"id": "co-real", "name": "Metsä Export Oy", "business_id": "2840123-6", "kyc_status": "approved",
                      "sanctions_status": "clear"}
    await db.raw.companies.insert_many([dict(W["sandbox_co"]), dict(W["normal_co"])])
    W["golf_user"] = {"id": "u-golf", "email": "gina@golfsea.test", "name": "Gina Golf", "org_type": "partner", "role": "partner",
                      "carrier_id": W["GOLF"], "carrier_name": "Golf Sea", "locale": "fi"}
    await db.raw.users.insert_one(dict(W["golf_user"]))


async def make_shipment(company: dict, tag: str) -> dict:
    from core.db import db
    from core.util import now
    from services import shipments as svc

    etd = (now() + timedelta(days=5)).date().isoformat()
    eta = (now() + timedelta(days=8)).date().isoformat()
    option = {"id": f"opt_{tag}", "mode": "sea", "service": "FCL", "carrier_id": W["ALPHA"], "carrier_name": "Alpha Lines",
              "rate_card_id": "card-alpha", "etd": etd, "eta": eta, "total_cents": 210000, "cost_cents": 165000,
              "liability_regime": "HAGUE_VISBY", "forwarder_role": "principal",
              "legs": [{"seq": 1, "kind": "main", "role": "main", "mode": "sea", "from": dict(FIHEL), "to": dict(DEHAM),
                        "carrier_id": W["ALPHA"], "carrier_name": "Alpha Lines", "carrier": "Alpha Lines", "transit_days": 3,
                        "service": "FCL", "rate_card_id": "card-alpha"}],
              "lines": [{"code": "OCEAN_FREIGHT", "stage": "main", "leg_seq": 1, "qty": 1, "unit_cost_cents": 150000,
                         "cost_cents": 150000, "unit_price_cents": 177000, "amount_cents": 177000, "is_pass_through": False},
                        {"code": "DG", "stage": "main", "leg_seq": 1, "qty": 1, "unit_cost_cents": 15000, "cost_cents": 15000,
                         "unit_price_cents": 17700, "amount_cents": 17700, "is_pass_through": False},
                        {"code": "DOC_FEE", "stage": "fees", "leg_seq": None, "qty": 1, "unit_cost_cents": 0, "cost_cents": 0,
                         "unit_price_cents": 3500, "amount_cents": 3500, "is_pass_through": False}]}
    quote = {"id": f"q_{tag}", "number": f"Q-{tag}", "company_id": company["id"], "status": "booked", "source": "ops",
             "request": {"mode": "sea", "origin": dict(FIHEL), "destination": dict(DEHAM), "cargo": dict(CARGO),
                         "direction": "export", "incoterm": "FCA", "customs_clearance": False, "ready_date": etd},
             "options": [option], "accepted_option_id": option["id"]}
    await db.raw.quotes.insert_one(dict(quote))
    booking = {"parties": {"shipper": {"name": "Metsä Export Oy", "address": {"street": "Satamatie 1", "city": "Helsinki",
                                                                               "postal_code": "00980", "country": "FI"}},
                           "consignee": {"name": "Hafen Handel GmbH", "address": {"street": "Kaiweg 2", "city": "Hamburg",
                                                                                  "postal_code": "20457", "country": "DE"}}},
               "references": {"customer_ref": "PO-4411", "po_numbers": ["4411"]}}
    return await svc.create_shipment(company_id=company["id"], quote=quote, option=option, booking=booking, actor=OPS)


async def _setup():
    # first import of core.db inside the module's loop: the Motor client binds to the loop that is running
    from core.db import ensure_indexes
    from core.events import on
    import services.carrier_booking  # noqa: F401  (registers the carrier_bookings indexes)

    @on("carrier.booked")
    async def _capture(payload):
        BOOKED.append(payload)

    await ensure_indexes()
    await _seed()


async def _teardown():
    from core.db import client

    await client.drop_database(os.environ["DB_NAME"])


@pytest.fixture(scope="module", autouse=True)
def world():
    run(_setup())
    yield W
    run(_teardown())


def _booked_for(shipment_id: str) -> list:
    return [p for p in BOOKED if p["shipment"]["id"] == shipment_id]


async def _fresh_leg(shipment_id: str, seq: int = 1) -> dict:
    from core.db import db

    s = await db.raw.shipments.find_one({"id": shipment_id}, {"_id": 0})
    return next(lg for lg in s["legs"] if lg["seq"] == seq)


def test_sandbox_booking_confirms_once_and_updates_the_leg():
    from core.db import db
    from services import carrier_booking as cb

    s = run(make_shipment(W["sandbox_co"], "sbx1"))
    planned = s["legs"][0]
    b1 = run(cb.book_leg(s["id"], 1, W["ALPHA"], OPS, reason="first booking"))
    b2 = run(cb.book_leg(s["id"], 1, W["ALPHA"], OPS, reason="first booking"))
    assert b1["id"] == b2["id"]
    assert (b1["status"], b1["mode"], b1["adapter"], b1["sandbox"]) == ("confirmed", "sandbox", "sandbox", True)
    assert re.fullmatch(r"SBX-ALPHA-\d{6}", b1["carrier_ref"]), b1["carrier_ref"]
    assert re.fullmatch(r"SBXALPHA\d{8}", b1["transport_doc_no"]), b1["transport_doc_no"]
    assert b1["vessel"] == {"name": "SANDBOX ALPHA 1", "imo": None, "mmsi": None}
    assert b1["etd"] == planned["etd"] and b1["eta"] == planned["eta"]
    assert b1["cost_cents"] == 165000  # quoted freight + DG lines of the accepted option, not the document fee
    assert run(db.raw.carrier_bookings.count_documents({"shipment_id": s["id"]})) == 1
    leg = run(_fresh_leg(s["id"]))
    assert leg["carrier_id"] == W["ALPHA"] and leg["carrier"] == "Alpha Lines"
    assert leg["transport_doc_no"] == b1["transport_doc_no"] and leg["vessel"]["name"] == "SANDBOX ALPHA 1"
    assert leg["voyage"] == b1["voyage"] and leg["etd"] == planned["etd"]
    assert leg["booking"]["id"] == b1["id"] and leg["booking"]["status"] == "confirmed" and leg["booking"]["mode"] == "sandbox"
    events = run(db.raw.tracking_events.find({"shipment_id": s["id"], "carrier_booking_id": b1["id"]}, {"_id": 0}).to_list(10))
    assert len(events) == 1 and events[0]["code"] == "note" and events[0]["source"] == "sandbox"
    assert events[0]["note_i18n"]["fi"].startswith("Osuus 1 varattu") and "(testiympäristö)" in events[0]["note_i18n"]["fi"]
    assert len(_booked_for(s["id"])) == 1
    assert run(db.raw.email_outbox.count_documents({"ref.booking_id": b1["id"]})) == 0


def test_log_mode_emails_the_carrier_and_waits_for_confirmation():
    from core.db import db
    from services import carrier_booking as cb

    s = run(make_shipment(W["normal_co"], "log1"))
    b = run(cb.book_leg(s["id"], 1, W["ALPHA"], OPS, reason="first booking"))
    again = run(cb.book_leg(s["id"], 1, W["ALPHA"], OPS))
    assert again["id"] == b["id"]
    assert (b["status"], b["mode"], b["sandbox"]) == ("requested", "log", False)
    assert b["carrier_ref"] is None and b["transport_doc_no"] is None and b["confirmed_at"] is None
    mails = run(db.raw.email_outbox.find({"tag": "carrier_booking", "ref.booking_id": b["id"]}, {"_id": 0}).to_list(10))
    assert len(mails) == 1
    m = mails[0]
    assert m["to"] == "alpha.booking@carrier.test" and m["status"] == "logged" and m["company_id"] == "co-real"
    for needle in (s["reference"], "Helsinki (FIHEL) → Hamburg (DEHAM)", "UN1263", "luokka 3", "class 3", b["id"], "PO-4411",
                   "1 × 40' HC", "18 000 kg"):
        assert needle in m["text"], needle
    assert m["text"].index("pyytää varausta") < m["text"].index("requests a booking")  # Finnish first
    leg = run(_fresh_leg(s["id"]))
    assert leg["transport_doc_no"] is None and leg["booking"]["status"] == "requested" and leg["booking"]["ref"] is None
    assert _booked_for(s["id"]) == []

    c = run(cb.confirm_booking(b["id"], carrier_ref="ALPHA-778812", actor=OPS,
                               details={"transport_doc_no": "ALPHAHEL0001", "vessel": {"name": "ALPHA STAR", "imo": "9301234"},
                                        "voyage": "042N"}))
    assert c["status"] == "confirmed" and c["carrier_ref"] == "ALPHA-778812" and c["confirmed_at"]
    assert run(cb.confirm_booking(b["id"], carrier_ref="ALPHA-778812", actor=OPS))["status"] == "confirmed"
    leg = run(_fresh_leg(s["id"]))
    assert leg["transport_doc_no"] == "ALPHAHEL0001" and leg["vessel"]["name"] == "ALPHA STAR" and leg["voyage"] == "042N"
    assert leg["booking"] == {"id": b["id"], "status": "confirmed", "ref": "ALPHA-778812", "mode": "log",
                              "carrier_id": W["ALPHA"], "confirmed_at": c["confirmed_at"]}
    assert len(_booked_for(s["id"])) == 1
    ev = _booked_for(s["id"])[0]["booking"]  # the keys execution (s9) reads, shared/interfaces.md
    assert ev["carrier_booking_reference"] == "ALPHA-778812" and ev["voyage"] == "042N" and ev["vessel"]["name"] == "ALPHA STAR"
    assert ev["equipment"] == [{"type": "40HC", "iso_type": "45G1", "count": 1}] and ev["cutoffs"] == []
    assert run(db.raw.email_outbox.count_documents({"ref.booking_id": b["id"]})) == 1


def test_log_mode_without_contact_email_fails_without_inventing_anything():
    from core.db import db
    from services import carrier_booking as cb

    s = run(make_shipment(W["normal_co"], "nomail"))
    b = run(cb.book_leg(s["id"], 1, W["HOTEL"], OPS))
    assert b["status"] == "failed" and b["failure"]["code"] == "no_contact_email"
    assert b["failure"]["fi"].startswith("Kuljetusliikkeellä Hotel Maritime") and b["carrier_ref"] is None
    raw = run(db.raw.carrier_bookings.find_one({"id": b["id"]}))
    assert "active_key" not in raw  # the leg is free for another carrier
    assert run(db.raw.email_outbox.count_documents({"ref.booking_id": b["id"]})) == 0
    assert run(_fresh_leg(s["id"]))["booking"]["status"] == "failed"


def test_rebook_cancels_the_old_booking_and_books_a_different_carrier():
    from core.db import db
    from services import carrier_booking as cb

    s = run(make_shipment(W["normal_co"], "rb1"))
    old = run(cb.book_leg(s["id"], 1, W["ALPHA"], OPS))
    run(cb.confirm_booking(old["id"], carrier_ref="ALPHA-1", actor=OPS,
                           details={"transport_doc_no": "ALPHABL1", "vessel": {"name": "ALPHA STAR"}, "voyage": "7N"}))
    r = run(cb.rebook_leg(s["id"], 1, OPS, reason="Vessel omitted Helsinki"))
    assert r["outcome"] == "rebooked"
    assert r["cancelled"]["id"] == old["id"] and r["cancelled"]["status"] == "cancelled"
    raw_old = run(db.raw.carrier_bookings.find_one({"id": old["id"]}))
    assert raw_old["status"] == "cancelled" and "active_key" not in raw_old
    nb = r["booking"]
    assert nb["carrier_id"] == W["BRAVO"] and nb["status"] == "requested" and nb["mode"] == "log"
    assert run(db.raw.email_outbox.count_documents({"tag": "carrier_booking_cancel", "ref.booking_id": old["id"],
                                                    "to": "alpha.booking@carrier.test"})) == 1
    assert run(db.raw.email_outbox.count_documents({"tag": "carrier_booking", "ref.booking_id": nb["id"],
                                                    "to": "bravo.booking@carrier.test"})) == 1
    leg = run(_fresh_leg(s["id"]))
    assert leg["transport_doc_no"] is None and leg["vessel"]["name"] is None and leg["voyage"] is None
    assert leg["booking"]["id"] == nb["id"] and leg["booking"]["status"] == "requested"
    assert run(db.raw.carrier_bookings.count_documents({"shipment_id": s["id"], "status": {"$in": ["requested", "confirmed"]}})) == 1


def test_rebook_in_sandbox_then_next_departure_when_only_the_current_carrier_fits():
    from core.db import db
    from core.util import parse_iso
    from services import carrier_booking as cb

    s = run(make_shipment(W["sandbox_co"], "rb2"))
    run(cb.book_leg(s["id"], 1, W["ALPHA"], OPS))
    r = run(cb.rebook_leg(s["id"], 1, OPS, reason="Alpha cancelled the sailing"))
    assert r["outcome"] == "rebooked" and r["booking"]["carrier_id"] == W["BRAVO"] and r["booking"]["status"] == "confirmed"
    leg = run(_fresh_leg(s["id"]))
    assert leg["carrier_id"] == W["BRAVO"] and leg["transport_doc_no"].startswith("SBXBRAVO")
    etd0 = parse_iso(leg["etd"])
    # every other carrier is excluded or ineligible (Charlie's card expired, Delta does not take class 3)
    r2 = run(cb.rebook_leg(s["id"], 1, OPS, reason="Missed the cut-off",
                           exclude_carrier_ids=[W["ALPHA"], W["ECHO"], W["GOLF"], W["HOTEL"]]))
    assert r2["outcome"] == "next_departure" and r2["cancelled"]["carrier_id"] == W["BRAVO"]
    assert r2["booking"]["carrier_id"] == W["BRAVO"] and r2["booking"]["status"] == "confirmed"
    leg = run(_fresh_leg(s["id"]))
    assert parse_iso(leg["etd"]) - etd0 == timedelta(days=7)  # sea default sailing interval
    assert r2["booking"]["etd"] == leg["etd"] and leg["booking"]["id"] == r2["booking"]["id"]
    assert run(db.raw.carrier_bookings.count_documents({"shipment_id": s["id"], "status": {"$in": ["requested", "confirmed"]}})) == 1
    r3 = run(cb.rebook_leg(s["id"], 1, OPS, reason="nothing left", exclude_carrier_ids=[W[c] for c in CARRIERS]))
    assert r3["outcome"] == "no_eligible_carrier" and r3["booking"] is None
    assert run(db.raw.carrier_bookings.find_one({"id": r2["booking"]["id"]}))["status"] == "confirmed"  # nothing cancelled


def test_ranking_prefers_the_quoted_then_the_cheaper_reliable_carrier_and_checks_dg():
    from services import carrier_booking as cb

    s = run(make_shipment(W["normal_co"], "rank1"))
    leg = s["legs"][0]
    assert run(cb.leg_cost_cents(s, leg)) == 165000
    res = run(cb.rank_carriers(s, leg))
    rows = {r["carrier_id"]: r for r in res["ranked"]}
    assert res["choice"]["carrier_id"] == W["ALPHA"] and res["confidence"] == 0.95 and res["dangerous_goods"] is True
    assert rows[W["ALPHA"]]["quoted"] and rows[W["ALPHA"]]["cost_cents"] == 165000
    delta = rows[W["DELTA"]]
    assert delta["dg_ok"] is False and delta["lane_ok"] is True and delta["eligible"] is False
    assert any(x["code"] == "dg_not_accepted" and "VAK-luokkaa 3" in x["fi"] and "class 3" in x["en"] for x in delta["reasons"])
    charlie = rows[W["CHARLIE"]]
    assert charlie["lane_ok"] is False and charlie["eligible"] is False
    assert any(x["code"] == "lane_expired" for x in charlie["reasons"])
    assert rows[W["GOLF"]]["mode"] == "partner_portal" and rows[W["BRAVO"]]["mode"] == "log"

    res2 = run(cb.rank_carriers(s, leg, exclude={W["ALPHA"]}))
    order = [r["carrier_id"] for r in res2["ranked"] if r["eligible"]]
    assert res2["choice"]["carrier_id"] == W["BRAVO"] and order[:2] == [W["BRAVO"], W["ECHO"]]
    assert res2["choice"]["cost_cents"] == 120000 and res2["choice"]["on_time_pct"] == 90
    assert next(r for r in res2["ranked"] if r["carrier_id"] == W["ECHO"])["cost_cents"] == 115000  # cheaper, unreliable
    assert 0.85 <= res2["confidence"] <= 0.95
    assert next(r for r in res2["ranked"] if r["carrier_id"] == W["ALPHA"])["excluded"] is True
    assert res2["ranked"][0]["score"] > res2["ranked"][1]["score"]


def test_partner_portal_request_is_confirmed_by_the_partner_accept_endpoint():
    from core.db import db
    from routes import partner
    from services import carrier_booking as cb

    s = run(make_shipment(W["normal_co"], "pp1"))
    b = run(cb.book_leg(s["id"], 1, W["GOLF"], OPS))
    assert (b["status"], b["mode"], b["adapter"]) == ("requested", "partner_portal", "partner_portal")
    leg = run(_fresh_leg(s["id"]))
    assert leg["carrier_id"] == W["GOLF"]  # assigned, so the partner can see and accept it
    notes = run(db.raw.notifications.find({"user_id": "u-golf", "link": f"/partner/shipments/{s['id']}"}, {"_id": 0}).to_list(5))
    assert len(notes) == 1 and notes[0]["title_i18n"]["fi"].startswith("Uusi kuljetuspyyntö")
    run(partner.partner_accept(s["id"], 1, partner.AcceptIn(truck_plate="abc-123"), user=W["golf_user"]))
    done = run(db.raw.carrier_bookings.find_one({"id": b["id"]}, {"_id": 0}))
    assert done["status"] == "confirmed" and done["carrier_ref"] == f"PARTNER-{b['id'][:8].upper()}"
    leg = run(_fresh_leg(s["id"]))
    assert leg["booking"]["status"] == "confirmed" and leg["truck_plate"] == "ABC-123" and leg["accepted_at"]
    assert len(_booked_for(s["id"])) == 1


def test_adapter_selection(monkeypatch):
    from core.db import db
    from services import carrier_booking as cb

    alpha = run(db.raw.carriers.find_one({"id": W["ALPHA"]}, {"_id": 0}))
    golf = run(db.raw.carriers.find_one({"id": W["GOLF"]}, {"_id": 0}))
    assert isinstance(run(cb.adapter_for(alpha, W["sandbox_co"])), cb.SandboxAdapter)
    assert isinstance(run(cb.adapter_for(alpha, W["normal_co"])), cb.LogAdapter)
    assert isinstance(run(cb.adapter_for(golf, W["normal_co"])), cb.PartnerPortalAdapter)
    monkeypatch.setenv("CARRIER_ALPHA_BASE_URL", "https://dcsa.alpha.test")
    monkeypatch.setenv("CARRIER_ALPHA_API_KEY", "key-1")
    monkeypatch.setenv("CARRIER_ALPHA_ENABLED", "true")
    assert isinstance(run(cb.adapter_for(alpha, W["normal_co"])), cb.LogAdapter)  # default mode log: no live API calls
    monkeypatch.setenv("CARRIER_BOOKING_MODE", "live")
    assert isinstance(run(cb.adapter_for(alpha, W["normal_co"])), cb.DcsaBookingAdapter)
    assert isinstance(run(cb.adapter_for(alpha, W["sandbox_co"])), cb.SandboxAdapter)
    monkeypatch.setenv("CARRIER_ALPHA_ENABLED", "false")
    assert isinstance(run(cb.adapter_for(alpha, W["normal_co"])), cb.LogAdapter)
    monkeypatch.setenv("CARRIER_BOOKING_MODE", "sandbox")
    assert isinstance(run(cb.adapter_for(alpha, W["normal_co"])), cb.SandboxAdapter)


def test_sandbox_transport_documents_per_mode():
    from services import carrier_booking as cb

    car = {"id": "c", "code": "FINNAIR", "name": "Finnair Cargo"}
    leg = {"seq": 1, "etd": "2026-11-01T08:00:00Z", "eta": "2026-11-02T08:00:00Z"}
    docs = {m: run(cb.SandboxAdapter(car).book({"booking_id": "b-1", "leg": {**leg, "mode": m}}))["transport_doc_no"]
            for m in ("air", "road", "rail", "sea")}
    assert re.fullmatch(r"105-\d{8}", docs["air"]) and cb.awb_check_ok(docs["air"])
    assert int(docs["air"][4:11]) % 7 == int(docs["air"][11])
    assert cb.awb_number("105", 1234567) == "105-12345675" and not cb.awb_check_ok("105-12345674")
    assert re.fullmatch(r"SBXCMR\d{8}", docs["road"]) and re.fullmatch(r"SBXCIM\d{8}", docs["rail"])
    assert re.fullmatch(r"SBXFINNAIR\d{8}", docs["sea"])


def _validator():
    return jsonschema.Draft202012Validator({"$defs": DCSA_CREATE_BOOKING_DEFS, "$ref": "#/$defs/CreateBooking"})


def test_dcsa_request_builder_produces_the_published_createbooking_shape():
    from services import carrier_booking as cb

    s = run(make_shipment(W["normal_co"], "dcsa1"))
    leg = s["legs"][0]
    alpha = {"id": W["ALPHA"], "code": "ALPHA", "name": "Alpha Lines"}
    body = cb.dcsa_booking_request(s, leg, alpha, booking_id="0f8c2a4e-1111-2222-3333-444455556666",
                                   contract_ref="HUO-Q-2026-001")
    errors = [f"{list(e.path)}: {e.message}" for e in _validator().iter_errors(body)]
    assert not errors, errors
    assert set(DCSA_CREATE_BOOKING_DEFS["CreateBooking"]["required"]) <= set(body)
    assert body["shipmentLocations"] == [
        {"location": {"locationName": "Helsinki", "UNLocationCode": "FIHEL"}, "locationTypeCode": "POL"},
        {"location": {"locationName": "Hamburg", "UNLocationCode": "DEHAM"}, "locationTypeCode": "POD"}]
    assert (body["receiptTypeAtOrigin"], body["deliveryTypeAtDestination"]) == ("CY", "CY")
    assert body["contractQuotationReference"] == "HUO-Q-2026-001" and "serviceContractReference" not in body
    assert body["references"] == [{"type": "CR", "value": s["reference"]}] and body["incoTerms"] == "FCA"
    assert body["expectedDepartureDate"] == leg["etd"][:10] and body["isExportDeclarationRequired"] is False
    eq = body["requestedEquipments"][0]
    assert (eq["ISOEquipmentCode"], eq["units"], eq["isShipperOwned"]) == ("45G1", 1, False)
    dg = eq["commodities"][0]["outerPackaging"]["dangerousGoods"][0]
    assert (dg["UNNumber"], dg["imoClass"], dg["packingGroup"], dg["properShippingName"]) == ("1263", "3", 2, "PAINT")
    assert body["documentParties"]["shipper"]["address"]["countryCode"] == "FI"
    with pytest.raises(ValueError):
        cb.dcsa_booking_request(s, leg, alpha, booking_id="x", contract_ref=None)
    with pytest.raises(ValueError):
        cb.dcsa_booking_request(s, {**leg, "mode": "road"}, alpha, booking_id="x", contract_ref="Q1")


def test_dcsa_adapter_posts_polls_and_maps_the_published_responses():
    from services import carrier_booking as cb

    s = run(make_shipment(W["normal_co"], "dcsa2"))
    leg = s["legs"][0]
    seen: list = []
    answers = {"GET": DCSA_PENDING_EXAMPLE}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"carrierBookingRequestReference": "cbrr-123"})
        if request.method == "PATCH":
            return httpx.Response(202)
        return httpx.Response(200, json=answers["GET"])

    carrier = {"id": W["ALPHA"], "code": "ALPHA", "name": "Alpha Lines"}
    ad = cb.DcsaBookingAdapter(carrier, "https://dcsa.alpha.test/bkg/", "key-1", contract_ref="HUO-Q-1",
                               transport=httpx.MockTransport(handler))
    res = run(ad.book({"booking_id": "b-dcsa-1", "shipment": s, "leg": leg, "carrier": carrier}))
    assert res["status"] == "requested" and res["carrier_request_ref"] == "cbrr-123"
    post = seen[0]
    assert post.method == "POST" and post.url.path == "/bkg/v2/bookings"
    assert post.headers["API-Version"] == "2.0.0" and post.headers["Authorization"] == "Bearer key-1"
    import json as _json
    assert not list(_validator().iter_errors(_json.loads(post.content)))
    pending = run(ad.status({"status": "requested", "carrier_request_ref": "cbrr-123", "_leg": leg}))
    assert pending["status"] == "requested" and seen[1].url.path == "/bkg/v2/bookings/cbrr-123"
    answers["GET"] = DCSA_CONFIRMED_EXAMPLE
    st = run(ad.status({"status": "requested", "carrier_request_ref": "cbrr-123", "_leg": leg}))
    assert st["status"] == "confirmed" and st["carrier_ref"] == "cbr-987" and st["voyage"] == "403W"
    assert st["vessel"] == {"name": "MAERSK IOWA", "imo": "9298686", "mmsi": None}
    assert st["etd"][:10] == "2024-01-20" and st["eta"][:10] == "2024-01-31"
    answers["GET"] = {**DCSA_CONFIRMED_EXAMPLE, "bookingStatus": "REJECTED", "feedbacks": [{"message": "No space"}]}
    rj = run(ad.status({"status": "requested", "carrier_request_ref": "cbrr-123", "_leg": leg}))
    assert rj["status"] == "rejected" and "No space" in rj["failure"]["en"]
    cancel = run(ad.cancel({"status": "confirmed", "carrier_ref": "cbr-987", "carrier_request_ref": "cbrr-123",
                            "_cancel_reason": "Shipper cancelled"}))
    patch = seen[-1]
    assert cancel["status"] == "cancelled" and patch.method == "PATCH" and patch.url.path == "/bkg/v2/bookings/cbr-987"
    assert _json.loads(patch.content) == {"bookingCancellationStatus": "CANCELLATION_RECEIVED", "reason": "Shipper cancelled"}


def test_dcsa_cutoff_times_are_kept_in_dcsa_codes():
    from services import carrier_booking as cb

    data = {**DCSA_CONFIRMED_EXAMPLE, "shipmentCutOffTimes": [
        {"cutOffDateTimeCode": "DCO", "cutOffDateTime": "2024-01-18T12:00:00Z"},
        {"cutOffDateTimeCode": "FCO", "cutOffDateTime": "2024-01-19T08:00:00Z"}, {"cutOffDateTimeCode": "VCO"}]}
    d = cb._dcsa_details(data, {"etd": None, "eta": None})
    assert d["cutoffs"] == [{"code": "DCO", "at": "2024-01-18T12:00:00Z", "source": "carrier"},
                            {"code": "FCO", "at": "2024-01-19T08:00:00Z", "source": "carrier"}]
    assert "cutoffs" not in cb._dcsa_details(DCSA_CONFIRMED_EXAMPLE, {"etd": None, "eta": None})


def test_dcsa_booking_specific_auth_selects_adapter_without_schedules_config(monkeypatch):
    from services import carrier_booking as cb
    carrier = {"id": W["ALPHA"], "code": "ALPHA", "integration": {"type": "dcsa"}}
    for suffix in ("BASE_URL", "API_KEY"):
        monkeypatch.delenv("CARRIER_ALPHA_" + suffix, raising=False)
    monkeypatch.setenv("CARRIER_BOOKING_MODE", "live")
    monkeypatch.setenv("CARRIER_ALPHA_ENABLED", "true")
    monkeypatch.setenv("CARRIER_ALPHA_BOOKING_URL", "https://conformance.example.test/api")
    monkeypatch.setenv("CARRIER_ALPHA_BOOKING_AUTH_HEADER", "dcsa-conformance-api-key")
    monkeypatch.setenv("CARRIER_ALPHA_BOOKING_AUTH_VALUE", "test-key")
    adapter = run(cb.adapter_for(carrier, W["normal_co"]))
    assert isinstance(adapter, cb.DcsaBookingAdapter)
    assert adapter._headers()["dcsa-conformance-api-key"] == "test-key"
    assert "Authorization" not in adapter._headers()
    monkeypatch.setenv("CARRIER_ALPHA_ENABLED", "false")
    assert not cb._dcsa_live(carrier)


def test_dcsa_confirmation_requires_carrier_reference():
    from services import carrier_booking as cb
    def handler(request):
        return httpx.Response(200, json={"bookingStatus": "CONFIRMED", "carrierBookingRequestReference": "request-only"})
    adapter = cb.DcsaBookingAdapter({}, "https://carrier.example.test", "test-key", transport=httpx.MockTransport(handler))
    result = run(adapter.status({"status": "requested", "carrier_request_ref": "request-only"}))
    assert result["status"] == "requested"
    assert "carrierBookingReference" in result["error"]


def test_official_test_api_refuses_production_and_preserves_demo_provenance(monkeypatch):
    from services import carrier_booking as cb
    from fastapi import HTTPException
    carrier = {"id": W["ALPHA"], "code": "ALPHA", "integration": {"type": "dcsa"}}
    monkeypatch.setenv("CARRIER_ALPHA_BOOKING_TEST_API", "true")
    monkeypatch.setenv("CARRIER_ALPHA_ENABLED", "true")
    monkeypatch.setenv("CARRIER_ALPHA_BOOKING_URL", "https://official.example.test/api")
    monkeypatch.setenv("CARRIER_ALPHA_API_KEY", "test-key")
    with pytest.raises(HTTPException) as exc:
        run(cb.adapter_for(carrier, W["normal_co"], shipment={"demo": False}))
    assert exc.value.status_code == 409
    demo = {"demo": True, "cargo": {"containers": [{"type": "20GP", "count": 1}]}}
    adapter = run(cb.adapter_for(carrier, W["sandbox_co"], shipment=demo, leg={"mode": "sea"}))
    assert isinstance(adapter, cb.DcsaBookingAdapter) and adapter.test_api
    with pytest.raises(HTTPException):
        run(adapter.book({"company": W["normal_co"], "shipment": {"demo": False}}))
    monkeypatch.setenv("CARRIER_ALPHA_BOOKING_TEST_API", "false")
    assert isinstance(run(cb.adapter_for(carrier, W["sandbox_co"], shipment=demo, leg={"mode": "sea"})), cb.SandboxAdapter)


def test_official_test_api_routes_require_operator_and_saved_test_identity(monkeypatch):
    from routes.shipments import carrier_test_book, carrier_test_status, CarrierTestBookingBody
    from core.db import db
    from fastapi import HTTPException
    s = run(make_shipment(W["normal_co"], "production-test-api-denial"))
    body = CarrierTestBookingBody(leg_seq=1, carrier_id=W["ALPHA"])
    monkeypatch.setenv("CARRIER_ALPHA_BOOKING_TEST_API", "true")
    with pytest.raises(HTTPException) as denied:
        run(carrier_test_book(s['id'], body, {"org_type": "customer", "company_id": s['company_id']}))
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied:
        run(carrier_test_book(s['id'], body, OPS))
    assert denied.value.status_code == 409
    assert run(db.raw.carrier_bookings.count_documents({'shipment_id': s['id']})) == 0
    with pytest.raises(HTTPException) as missing:
        run(carrier_test_status(s['id'], 'unrelated-booking', OPS))
    assert missing.value.status_code == 404


def test_test_booking_cannot_be_polled_after_provider_environment_switch(monkeypatch):
    from services import carrier_booking as cb
    from core.db import db
    from fastapi import HTTPException
    s = run(make_shipment(W['sandbox_co'], 'test-env-switch'))
    b = {'id': 'test-api-stale-config', 'shipment_id': s['id'], 'carrier_id': W['ALPHA'],
         'adapter': 'dcsa', 'provider_environment': 'test', 'status': 'requested', 'leg_seq': 1}
    run(db.raw.carrier_bookings.insert_one(dict(b)))
    monkeypatch.setenv('CARRIER_ALPHA_BOOKING_TEST_API', 'false')
    with pytest.raises(HTTPException) as denied:
        run(cb.poll_booking(b['id']))
    assert denied.value.status_code == 409


def test_demo_label_cannot_override_production_or_missing_owner():
    from services import carrier_booking as cb
    demo = {'demo': True}
    assert not cb._test_data(None, demo)
    assert not cb._test_data({'id': 'production'}, demo)
    assert not cb._test_data({'id': 'production', 'test_tenant': False, 'demo': True}, demo)
    assert not cb._test_data({'test_tenant': 'true'}, demo)
    assert cb._test_data({'id': 'demo', 'demo': True}, demo)
    assert cb._test_data({'id': 'test', 'test_tenant': True}, {'demo': False})


def test_official_test_failure_and_cancellation_do_not_dispatch_production_events(monkeypatch):
    from services import carrier_booking as cb
    from core.db import db
    events = []
    async def capture(name, payload): events.append((name, payload))
    monkeypatch.setattr(cb, 'emit', capture)
    s = run(make_shipment(W['sandbox_co'], 'test-event-cancel'))
    b = run(cb.book_leg(s['id'], 1, W['ALPHA'], OPS))
    events.clear()  # legacy simulator events remain unchanged
    run(db.raw.carrier_bookings.update_one({'id': b['id']}, {'$set': {'provider_environment': 'test'}}))
    run(cb.cancel_booking(b['id'], OPS, 'Local test cancellation'))
    assert [name for name, _ in events] == ['carrier.test_booking_cancelled']
    assert events[0][1]['shipment']['id'] == s['id']
    receipt = run(db.raw.carrier_test_events.find_one({'booking_id': b['id']}))
    assert receipt['event'] == 'carrier.test_booking_cancelled'
    assert receipt['dispatch_status'] == 'dispatched' and receipt['payload'] == events[0][1]
    events.clear()
    s = run(make_shipment(W['normal_co'], 'test-event-failure'))
    b = run(cb.book_leg(s['id'], 1, W['ALPHA'], OPS))
    run(db.raw.carrier_bookings.update_one({'id': b['id']}, {'$set': {'provider_environment': 'test'}}))
    b = run(cb._booking(b['id']))
    run(cb._finish(b, 'failed', OPS))
    assert [name for name, _ in events] == ['carrier.test_booking_failed']
    assert events[0][1]['booking']['provider_environment'] == 'test'
    receipt = run(db.raw.carrier_test_events.find_one({'booking_id': b['id']}))
    assert receipt['event'] == 'carrier.test_booking_failed'
    assert receipt['dispatch_status'] == 'dispatched' and receipt['payload'] == events[0][1]


def test_test_confirmation_audit_is_durable_scoped_and_readable_when_disabled(monkeypatch):
    from core.db import db
    from core.deps import current_user
    from fastapi import FastAPI
    from routes.shipments import router
    from services import carrier_booking as cb
    events = []
    async def capture(name, payload): events.append((name, payload))
    monkeypatch.setattr(cb, 'emit', capture)
    s = run(make_shipment(W['normal_co'], 'durable-test-confirmation'))
    b = run(cb.book_leg(s['id'], 1, W['ALPHA'], OPS))
    # Local fixture only: test state is not written to an external/live app.
    run(db.raw.carrier_bookings.update_one({'id': b['id']}, {'$set': {'provider_environment': 'test', 'sandbox': True}}))
    b = run(cb._booking(b['id']))
    run(cb._confirm(b, {'carrier_ref': 'TEST-DURABLE-1'}, OPS))
    run(cb._confirm(b, {'carrier_ref': 'TEST-DURABLE-1'}, OPS))
    assert [name for name, _ in events] == ['carrier.test_booked']
    app = FastAPI(); app.include_router(router)
    actor = dict(OPS, permissions=['shipments.read_all'])
    async def identity(): return actor
    app.dependency_overrides[current_user] = identity
    async def get(path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.get(path)
    route = f"/api/shipments/{s['id']}/carrier-test-bookings/{b['id']}/events"
    monkeypatch.setenv('CARRIER_ALPHA_ENABLED', 'false')
    response = run(get(route))
    assert response.status_code == 200
    data = response.json()
    assert data['counts'] == {'carrier.test_booked': 1}
    assert data['total'] == 1 and data['pending'] == 0 and data['production_event_count'] == 0
    assert data['items'][0]['payload'] == events[0][1]
    assert data['items'][0]['payload']['booking']['provider_environment'] == 'test'
    other = run(make_shipment(W['normal_co'], 'wrong-audit-shipment'))
    assert run(get(route.replace(s['id'], other['id']))).status_code == 404
    actor = {'id':'customer-audit','role':'customer_admin','org_type':'customer','company_id':s['company_id'],
             'permissions':['shipments.read_all']}
    assert run(get(route)).status_code == 403
    actor = dict(OPS, permissions=[])
    assert run(get(route)).status_code == 403
    actor = dict(OPS)
    # Genuine production dispatch still uses its original name and creates no test receipt.
    events.clear()
    prod = run(cb.book_leg(other['id'], 1, W['ALPHA'], OPS))
    run(cb._confirm(prod, {'carrier_ref':'PRODUCTION-BASELINE'}, OPS))
    assert [name for name, _ in events] == ['carrier.booked']
    assert run(db.raw.carrier_test_events.count_documents({'booking_id':prod['id']})) == 0


def test_interrupted_test_dispatch_stays_pending_not_successful(monkeypatch):
    from core.db import db
    from services import carrier_booking as cb
    s = run(make_shipment(W['normal_co'], 'pending-test-event'))
    b = run(cb.book_leg(s['id'], 1, W['ALPHA'], OPS))
    b['provider_environment'] = 'test'; b['sandbox'] = True
    async def broken_emit(name, payload): raise RuntimeError('Interrupted test dispatcher')
    monkeypatch.setattr(cb, 'emit', broken_emit)
    with pytest.raises(RuntimeError):
        run(cb._emit_booking_event('carrier.booked', {'shipment':s, 'booking':b}))
    receipt = run(db.raw.carrier_test_events.find_one({'booking_id':b['id']}))
    assert receipt['dispatch_status'] == 'pending' and 'dispatched_at' not in receipt


def test_booking_email_reply_uses_real_inbound_and_confirmation(monkeypatch):
    from email.message import EmailMessage
    from core.db import db
    from core.settings import update_settings
    from services import carrier_booking as cb, carrier_booking_mail as mail
    from services.email_channel.dispatch import dispatch_raw
    run(update_settings({"autopilot_enabled": True, "autopilot_halted": False}))
    s = run(make_shipment(W["normal_co"], "email-confirm"))
    b = run(cb.book_leg(s["id"], 1, W["ALPHA"], OPS))
    assert b["request"]["message_id"].startswith("<")

    async def extract(*args, **kwargs):
        return {"intent": "confirmed", "confidence": 0.99, "carrier_ref": "ALPHA-MAIL-123",
                "cost_cents": None, "details": {"transport_doc_no": "ALPHA-BL-123"}}
    monkeypatch.setattr(mail, "llm_json", extract)
    m = EmailMessage()
    m["From"], m["To"] = "alpha.booking@carrier.test", "ops@huolitsija.test"
    m["Subject"], m["Message-ID"] = "Re: " + b["request"]["subject"], "<booking-confirm-123@carrier.test>"
    m["In-Reply-To"] = b["request"]["message_id"]
    m.set_content("Confirmed ALPHA-MAIL-123. B/L ALPHA-BL-123.")
    result = run(dispatch_raw(m.as_bytes(), provider_authenticated=True))
    assert result["channel"] == "carrier_booking" and result["status"] == "received"
    assert run(mail.process({"payload": {"message_id": result["id"]}}))["status"] == "confirmed"
    updated = run(db.raw.carrier_bookings.find_one({"id": b["id"]}))
    assert updated["status"] == "confirmed" and updated["carrier_ref"] == "ALPHA-MAIL-123"
    assert run(_fresh_leg(s["id"]))["transport_doc_no"] == "ALPHA-BL-123"
    assert run(dispatch_raw(m.as_bytes(), provider_authenticated=True))["status"] == "duplicate"


def test_booking_reply_refuses_wrong_sender_and_untrusted_provider():
    from email.message import EmailMessage
    from core.db import db
    from services import carrier_booking as cb, carrier_booking_mail as mail
    s = run(make_shipment(W["normal_co"], "email-sender"))
    b = run(cb.book_leg(s["id"], 1, W["ALPHA"], OPS))
    for sender, authenticated, expected in [("attacker@elsewhere.test", True, "sender_mismatch"),
                                             ("alpha.booking@carrier.test", False, "unauthenticated")]:
        m = EmailMessage()
        m["From"], m["To"] = sender, "ops@huolitsija.test"
        m["Subject"] = "Re: " + b["request"]["subject"]
        m["Message-ID"] = f"<{expected}@carrier.test>"
        m.set_content("Confirmed ATTACKER-123")
        result = run(mail.try_ingest(m.as_bytes(), authenticated=authenticated))
        assert result["status"] == expected
    assert run(db.raw.carrier_bookings.find_one({"id": b["id"]}))["status"] == "requested"
