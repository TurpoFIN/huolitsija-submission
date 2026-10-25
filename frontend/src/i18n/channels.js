// s14: notification channels (SMS), shipper API, EDI and management insight strings live in their own files.
const en = {
  "channels.sms": "Text-message updates", "channels.sms_hint": "Pickup, departure, customs hold, delivery to {phone}",
  "channels.sms_needs_phone": "Save a phone number above first",
  "osettings.i.sms": "SMS (Twilio)", "channels.sms_on": "customer updates on", "channels.sms_off": "customer updates off",
  "osettings.f.sms_notifications_enabled": "Text-message shipment updates to customers who opt in",
};
const fi = {
  "channels.sms": "Tekstiviesti-ilmoitukset", "channels.sms_hint": "Nouto, lähtö, tullipysäytys ja toimitus numeroon {phone}",
  "channels.sms_needs_phone": "Tallenna ensin puhelinnumero yllä",
  "osettings.i.sms": "Tekstiviestit (Twilio)", "channels.sms_on": "asiakasilmoitukset päällä", "channels.sms_off": "asiakasilmoitukset pois",
  "osettings.f.sms_notifications_enabled": "Tekstiviesti-ilmoitukset asiakkaille, jotka ovat ottaneet ne käyttöön",
};
export default { en, fi };
