const en = [
  {id:"purpose",title:"Only storage for the service you request",blocks:[{p:"Huolitsija uses sign-in cookies and browser storage for the portal, your chosen preferences and booking continuity. The application does not load advertising or audience-analytics trackers. Non-essential tracking would require your prior consent; no such tracking is enabled by this notice. Payment pages opened at Stripe have their own notice."},{p:"The service provider and contact details are on [Company details](/company); personal-data processing is explained in the [Privacy notice](/privacy)."}]},
  {id:"inventory",title:"Storage inventory",blocks:[{table:{head:["Name / technology","Purpose and lifetime"],rows:[
    ["huo_session / HTTP-only cookie","Keeps you signed in. Secure in production, SameSite=Lax, path /. Default maximum age 14 days (deployment setting); removed on sign-out. Server-side revocation can end access earlier."],
    ["huo.locale; huo.theme / localStorage","Your language and display choice; until changed or site data is cleared."],
    ["huo.sidebar; huo.includeTestTenants / localStorage","Navigation and operator visibility choices; until changed or cleared."],
    ["huo.claim / localStorage","Connects your requested instant quote to your account; cleared after a successful claim or rejected/expired claim. The server claim link expires after 30 days; browser storage otherwise persists until cleared."],
    ["huo.claim.attempted / sessionStorage","Prevents repeated claim attempts in the same tab; ends when the tab session ends."],
    ["huo.booking.draft.<quote ID> / localStorage","Keeps booking details you enter, including contact details, so you can return to the form. Removed on successful booking or draft discard; otherwise remains until site data is cleared."]
  ]}}]},
  {id:"control",title:"Your controls",blocks:[{p:"Sign out to remove the session cookie. Clear this site's cookies and site data in your browser to remove stored drafts and preferences; this also signs you out. Disabling storage may prevent sign-in or draft recovery. On a shared device, discard drafts and clear site data after use. These storage items serve the functions you request, so a separate advertising-consent banner is not used."}]}
];
const fi = [
  {id:"purpose",title:"Tallennus pyytämääsi palvelua varten",blocks:[{p:"Huolitsija käyttää kirjautumisevästettä ja selaintallennusta portaaliin, valitsemiisi asetuksiin sekä varauksen jatkamiseen. Sovellus ei lataa mainonta- tai kävijäanalytiikan seurantaa. Ei-välttämätön seuranta edellyttäisi ennakkosuostumustasi; tämä ilmoitus ei ota sellaista käyttöön. Stripen maksusivuilla on oma ilmoituksensa."},{p:"Palveluntarjoaja ja yhteystiedot ovat [Yritystiedot](/company)-sivulla. Henkilötietojen käsittely kuvataan [tietosuojaselosteessa](/privacy)."}]},
  {id:"inventory",title:"Tallennettavat tiedot",blocks:[{table:{head:["Nimi / tekniikka","Tarkoitus ja kesto"],rows:[
    ["huo_session / HTTP-only-eväste","Ylläpitää kirjautumista. Tuotannossa Secure, SameSite=Lax, polku /. Oletuskesto enintään 14 päivää (palvelimen asetus); poistuu uloskirjautuessa. Istunnon voi perua palvelimella aiemmin."],
    ["huo.locale; huo.theme / localStorage","Valitsemasi kieli ja teema; kunnes muutat niitä tai poistat sivuston tiedot."],
    ["huo.sidebar; huo.includeTestTenants / localStorage","Navigoinnin ja operaattorin näkymän valinnat; kunnes muutat tai poistat ne."],
    ["huo.claim / localStorage","Liittää pyytämäsi pikatarjouksen tiliisi; poistuu onnistuneen liittämisen tai hylätyn/vanhentuneen linkin jälkeen. Palvelimen linkki vanhenee 30 päivässä, mutta selaintieto säilyy muuten poistamiseen asti."],
    ["huo.claim.attempted / sessionStorage","Estää saman liittämisyrityksen toiston välilehdellä; poistuu välilehti-istunnon päättyessä."],
    ["huo.booking.draft.<tarjouksen tunnus> / localStorage","Säilyttää syöttämäsi varaustiedot yhteystietoineen lomakkeelle palaamista varten. Poistuu onnistuneen varauksen tai luonnoksen hylkäämisen jälkeen; muuten säilyy sivuston tietojen poistamiseen asti."]
  ]}}]},
  {id:"control",title:"Tietojen hallinta",blocks:[{p:"Kirjaudu ulos poistaaksesi istuntoevästeen. Selaimen sivustotietojen ja evästeiden poistaminen poistaa luonnokset ja valinnat sekä kirjaa sinut ulos. Tallennuksen estäminen voi estää kirjautumisen tai luonnoksen palauttamisen. Yhteiskäyttöisellä laitteella hylkää luonnokset ja poista sivustotiedot käytön jälkeen. Tallennus palvelee pyytämiäsi toimintoja, joten erillistä mainonnan suostumusbanneria ei käytetä."}]}
];
export default {en, fi};
