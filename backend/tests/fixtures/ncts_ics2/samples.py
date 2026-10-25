"""Explicit synthetic test declarations. NOT captured authority responses or real filings."""
from copy import deepcopy
PARTY={'name':'Sandbox Logistics Oy','identificationNumber':'FI39900099',
       'address':{'city':'Helsinki','country':'FI','street':'Testikatu','postCode':'00100','number':'1'},
       'communication':[{'identifier':'sandbox@example.invalid','type':'EM'}]}
P={'name':'Sandbox Consignor','typeOfPerson':'2','address':{'city':'Oslo','country':'NO','street':'Test','postCode':'0010'}}

def transit():
    return {'lrn':'SANDBOX-NCTS-001','declaration_type':'T1','office_of_departure':'FI015300',
        'office_of_destination':'FI002000','guarantee_amount':'1000','guarantee_currency':'EUR',
        'transport':{'countryOfDispatch':'FI','countryOfDestination':'FI','containerIndicator':'0',
            'inlandModeOfTransport':'3','Consignor':{'name':'Sandbox Shipper','Address':{'streetAndNumber':'Test 1','postcode':'00100','city':'Helsinki','country':'FI'}},
            'Consignee':{'name':'Sandbox Consignee','Address':{'streetAndNumber':'Test 2','postcode':'00100','city':'Helsinki','country':'FI'}},
            'PlaceOfLoading':{'country':'FI','location':'Helsinki'},
            'DepartureTransportMeans':[{'sequenceNumber':1,'typeOfIdentification':'30','identificationNumber':'SBX-001','nationality':'FI'}]},
        'items':[{'description':'New bicycle frames','hs_code':'871491','gross_mass':'100','package_type':'CT','packages':2,'shipping_marks':'SBX'}],
        'seals':['SBX-SEAL-1'], 'parties':[{'name':'Sandbox Logistics Oy','eori':'FI39900099','parties':[{'name':'Sandbox Carrier','eori':'NO999999999'}]}]}


def ens(mode):
    party=deepcopy(P);consignee=deepcopy(P);consignee['name']='Sandbox Consignee'
    goods={'goodsItemNumber':1,'commodity':{'descriptionOfGoods':'New bicycle frames','commodityCode':{'harmonizedSystemSubHeadingCode':'871491'}},
            'weight':{'grossMass':'100'},'packaging':[{'typeOfPackages':'CT','numberOfPackages':2,'shippingMarks':'SBX'}]}
    routing=[{'sequenceNumber':1,'country':'NO'},{'sequenceNumber':2,'country':'FI'}]
    house={'containerIndicator':'0','totalGrossMass':'100','placeOfAcceptance':{'UNLOCODE':'NOOSL'},
        'consignee':consignee,'consignor':party,'goodsItem':[goods],
        'transportCharges':{'methodOfPayment':'H'},'placeOfDelivery':{'UNLOCODE':'FIHEL'},
        'countriesOfRoutingOfConsignment':routing,
        'transportDocumentHouseLevel':{'documentNumber':'SBX-HOUSE-001','type':'N714' if mode=='air' else 'N730'}}
    data={'LRN':'SANDBOX-ENS-001','documentIssueDate':{'DateTime':'2026-10-24T12:00:00Z'},
          'declarant':deepcopy(PARTY)}
    if mode in {'sea','air'}:
        house.update(transportDocumentMasterLevel={'documentNumber':'SBX-MASTER-001','type':'N740' if mode=='air' else 'N705'},
                     carrier={'identificationNumber':'NO999999999'})
        data.update(addressedMemberState={'country':'FI'},activeBorderTransportMeans={'ModeOfTransport':'1' if mode=='sea' else '4'},
            consignmentMasterLevel={'consignmentHouseLevel':[house]})
    else:
        transport={'typeOfMeansOfTransport':'2301' if mode=='rail' else '3100',
            'ModeOfTransport':'2' if mode=='rail' else '3',
            'estimatedDateAndTimeOfArrival':{'DateTime':'2026-10-25T12:00:00Z'},
            'countriesOfRoutingOfMeansOfTransport':routing}
        if mode=='rail':transport['conveyanceReferenceNumber']='SBX-TRAIN-1'
        else:
            transport.update(identificationNumber='SBX-001',typeOfIdentification='30',nationality='NO')
            goods['passiveBorderTransportMeans']=[{'identificationNumber':'SBX-001','typeOfIdentification':'30','nationality':'NO','typeOfMeansOfTransport':'3100'}]
        house.pop('consignee');house.pop('consignor')
        master={'placeOfAcceptance':{'UNLOCODE':'NOOSL'},'placeOfDelivery':{'UNLOCODE':'FIHEL'},
            'consignee':consignee,'consignor':party,'carrier':deepcopy(PARTY),
            'transportCharges':{'methodOfPayment':'H'},'consignmentHouseLevel':[house],
            'placeOfLoading':{'UNLOCODE':'NOOSL'},'placeOfUnloading':{'UNLOCODE':'FIHEL'},
            'transportDocumentMasterLevel':{'documentNumber':'SBX-MASTER-001','type':'N730'}}
        data.update(reEntryIndicator='0',activeBorderTransportMeans=transport,
            consignmentMasterLevel=master,customsOfficeOfFirstEntry={'referenceNumber':'FI015300'})
    ft={'sea':'IE3F15','air':'IE3F26','road':'IE3F50','rail':'IE3F51'}[mode]
    data['SpecificCircumstanceIndicator']=ft[3:]
    return {'lrn':'SANDBOX-ENS-001','mode':mode,'filing_type':ft,'ens_data':data,
        'deadline_inputs':{'arrival':'2026-10-25T12:00:00Z','loading':'2026-10-24T12:00:00Z',
            'departure':'2026-10-24T13:00:00Z','duration_hours':23,'sea_category':'short_sea'},
        'eori':'FI39900099','parties':[{'name':'Sandbox Carrier','eori':'NO999999999'}]}


def arrival(mrn):
    return {'TransitOperation':{'MRN':mrn,'arrivalNotificationDateAndTime':'2026-10-25T12:00:00','simplifiedProcedure':'1'},
        'CustomsOfficeOfDestinationActual':{'referenceNumber':'FI002000'},
        'TraderAtDestination':{'identificationNumber':'FI39900099'},
        'Consignment':{'LocationOfGoods':{'typeOfLocation':'A','qualifierOfIdentification':'V','CustomsOffice':{'referenceNumber':'FI002000'}}}}


def unloading(mrn):
    return {'TransitOperation':{'MRN':mrn},'CustomsOfficeOfDestinationActual':{'referenceNumber':'FI002000'},
        'TraderAtDestination':{'identificationNumber':'FI39900099'},
        'UnloadingRemark':{'conform':'1','unloadingCompletion':'1','unloadingDate':'2026-10-25','stateOfSeals':'1'}}


def national():
    return {'Identification':'SBX-NCTS-001','FunctionalReference':{'@function':'LRN','#text':'SANDBOX-NCTS-001'},
        'Type':[{'@function':'DeclarationType','#text':'T1'},{'@function':'AdditionalDeclarationType','#text':'A'}],
        'BindingItinerary':'0','Language':'FI','ReducedDatasetIndicator':'0','Security':'0',
        'Principal':{'Identification':'FI39900099','Role':'Y'},
        'Submitter':{'Name':'Sandbox Logistics','Communication':{'Identification':'sandbox@example.invalid','Type':'EM'}},
        'Consignment':{'TotalGrossMass':'100','LoadingLocation':{'Address':{'Country':'FI'},'Name':'Helsinki'},
            'HouseConsignment':{'Sequence':1,'TotalGrossMass':'100','ConsignmentItem':{
                'Sequence':1,'GoodsItemNumber':1,'Commodity':{'Description':'New bicycle frames'},
                'GoodsMeasure':{'GrossMass':'100'},'Packaging':{'Sequence':1,'Type':'CT','Quantity':2},
                'PreviousDocument':{'Sequence':1,'Type':'N820','Identification':'SBX-PREVIOUS'}}}},
        'Guarantee':{'Sequence':1,'Type':'1','GuaranteeReference':{'Sequence':1,
            'OtherGuaranteeReference':'26FI0000000000000','AccessCode':'TEST','Amount':{'@CurrencyUnit':'EUR','#text':'1000'}}},
        'DepartureOffice':{'Identification':'FI015300'},'DestinationOffice':{'Identification':'FI002000'}}
