// Bilingual legal notice. Sources and operating prerequisites: shared/s4/.
export const PRIVACY_VERSION = "1.1";
export const PRIVACY_DATE = "2026-10-24";
export default {
  "en": [
    {
      "id": "controller",
      "title": "Controller and contact",
      "blocks": [
        {
          "p": "The service provider is the controller. Its legal identity, registration status, postal address and privacy contact are published on [Company details](/company#contact). Incorporation is pending until explicitly confirmed there; the operator must identify the accountable controller before collecting production customer data."
        }
      ]
    },
    {
      "id": "data",
      "title": "Whose data we process, and what",
      "blocks": [
        {
          "ul": [
            "Contact persons and portal users at our customers: name, e-mail, phone, role, company, language, sign-in and security data (hashed password, sign-in times).",
            "Parties to shipments: names, addresses and contact details of shippers, consignees, notify parties, pickup and delivery contacts.",
            "Customs and trade documents you upload, such as commercial invoices, packing lists, transport documents and powers of attorney. They may contain personal data of contact persons and sole traders.",
            "Customer due diligence and sanctions screening: names of company representatives and the results of the checks.",
            "Payments: invoice and payment status. Card payments are handled by Stripe; we do not see or store full card numbers.",
            "Messages in the portal and e-mails with us.",
            "Website and instant-quote users: IP address (used to limit request rates and protect the service), the route and cargo you enter, and technical data in server logs. Instant quotes are stored without your name until you claim them to an account."
          ]
        }
      ]
    },
    {
      "id": "purposes",
      "title": "Purposes and legal bases",
      "blocks": [
        {
          "table": {
            "head": [
              "Purpose",
              "Legal basis (GDPR art. 6(1))"
            ],
            "rows": [
              [
                "Quotes, bookings, transport, customs services and the customer account",
                "Contract (b); for contact persons of business customers, legitimate interest (f)"
              ],
              [
                "Customs declarations and keeping customs records",
                "Legal obligation (c): EU customs law and the Finnish Customs Act"
              ],
              [
                "Accounting and invoicing",
                "Legal obligation (c): Accounting Act (1336/1997)"
              ],
              [
                "Customer due diligence and sanctions screening",
                "Legal obligation (c): EU sanctions regulations; legitimate interest (f) in preventing fraud and unlawful shipments"
              ],
              [
                "Security, abuse prevention and rate limiting of the public quote tool",
                "Legitimate interest (f)"
              ],
              [
                "Service messages: booking confirmations, tracking updates, invoices",
                "Contract (b)"
              ],
              [
                "News and offers to business contacts",
                "Legitimate interest (f), with an opt-out in every message; consent (a) where the law requires it"
              ],
              [
                "Handling claims and legal disputes",
                "Legitimate interest (f)"
              ]
            ]
          }
        }
      ]
    },
    {
      "id": "ai",
      "title": "AI-assisted document handling",
      "blocks": [
        {
          "p": "We use an AI model provider to read documents you upload (such as invoices and packing lists), extract their data and propose HS codes. When Autopilot is off, the authorised owner or service provider approve customer replies and customs filings. If the operator enables Autopilot, software may send replies, approve business quotes within limits, submit validated customs declarations through the configured channel, resolve exceptions and issue invoices. The operator can disable it, and actions are logged."
        },
        {
          "p": "The operational policy uses shipment facts, price limits, margin rules, approved lanes and mandatory validation checks. It does not score natural persons for credit or employment. You may ask the responsible owner for human review, an explanation or correction of an automated operational action; statutory rights under GDPR article 22 remain unaffected. The provider processes the documents on our behalf as a processor under GDPR art. 28."
        },
        {
          "p": "If a decision about a natural person has legal or similarly significant effects, we must establish an Article 22 exception and safeguards or arrange meaningful human review before that decision. Sole-trader accounts and personal guarantees need this assessment; a business account label does not remove individual rights. We do not rely on your use of this website as blanket consent to such decisions."
        }
      ]
    },
    {
      "id": "partner-sourcing",
      "title": "Partner sourcing (Sourcing Autopilot)",
      "blocks": [
        {
          "p": "To find carriers, hauliers and agents and to request freight quotes, we process business contact data that companies have published themselves: company name, business ID, website, and the business e-mail address shown on the company's own site (preferably a role address such as sales@). Sources are public registers (Finnish Trade Register / YTJ open data), Wikidata and company websites; we store the source address of every contact."
        },
        {
          "p": "Legal basis: our legitimate interest in buying transport for our customers (GDPR art. 6(1)(f)). We contact legal persons only, never sole traders or private addresses, and at most a limited number of messages per day. Every message identifies Huolitsija Oy, says where we found the address and contains a one-click link to stop further messages; a reply asking us to stop works too. We keep the opt-out list so that we do not contact you again."
        },
        {
          "p": "We keep contact and message records for 24 months after the last contact, and accepted rates with their source for as long as accounting and customs rules require. Replies are read by software, including an AI model provider acting as our processor. You can object, ask for access or erasure at [Privacy contact](/company#contact)."
        }
      ]
    },
    {
      "id": "recipients",
      "title": "Recipients",
      "blocks": [
        {
          "p": "Our deployed infrastructure uses Emergent hosting with MongoDB, OpenAI for automated document and message processing, and Stripe for payment checkout. Stripe can also act as an independent controller for its regulatory and fraud-prevention purposes. MongoDB is database software here, not evidence of a separate MongoDB Atlas contract."
        },
        {
          "table": {
            "head": [
              "Service",
              "Data / activation"
            ],
            "rows": [
              [
                "Emergent",
                "Application, database, documents and technical logs; hosting processor."
              ],
              [
                "OpenAI",
                "Selected message text and documents for extraction, replies and operational decisions; AI processor."
              ],
              [
                "Stripe",
                "Payment contact, invoice reference, amount and checkout data. Test payments are distinct from live payments."
              ],
              [
                "Postmark / Resend",
                "Email addresses, messages and delivery events, only when the selected delivery service is enabled. Log mode sends no email."
              ],
              [
                "Twilio",
                "Phone number, opted-in service SMS and delivery status, only when SMS delivery is enabled. Log mode sends no SMS."
              ],
              [
                "Redkik",
                "Shipment, cargo value and insurance contact data only if live insurance is configured and requested; insurer/intermediary roles follow the policy contract."
              ],
              [
                "OpenSanctions",
                "Names for PEP screening only when its paid API is configured. Public register lookups also use PRH/YTJ, VIES/EORI, GLEIF and Wikidata."
              ]
            ]
          }
        },
        {
          "p": "Carrier, warehouse, terminal, customs, tax and insurance recipients receive only data needed for the shipment or their legal duties and may be independent controllers. Optional integrations are not a statement that an account is approved or a policy exists. Ask the privacy contact for the current recipient list, contracting entities and safeguards."
        },
        {
          "p": "Map backgrounds are requested from OpenFreeMap. With the default configured geocoding/routing endpoints, address or coordinate queries go to OpenStreetMap Nominatim, Photon (komoot) and the OSRM routing service. These services receive the technical request data needed to respond; browser map requests also disclose the visitor’s network address. The configured endpoint and its terms determine the recipient for each lookup."
        }
      ]
    },
    {
      "id": "transfers",
      "title": "Transfers outside the EU/EEA",
      "blocks": [
        {
          "p": "Transport to or from a country outside the EU/EEA requires sharing shipment data with carriers, agents and authorities in that country. We share only what is needed to perform the transport you ordered."
        },
        {
          "p": "When a processor handles data outside the EU/EEA, we rely on an adequacy decision of the European Commission or on the Commission's standard contractual clauses."
        },
        {
          "p": "Hosting regions and each processor’s onward transfers must be confirmed in the applicable agreements. A provider name alone does not establish EU-only processing. Copies or details of the applicable safeguards are available through the privacy contact."
        }
      ]
    },
    {
      "id": "retention",
      "title": "How long we keep data",
      "blocks": [
        {
          "p": "Retention depends on purpose, the end of the relationship and statutory duties. Accounting books and financial statements: at least 10 years; vouchers: at least 6 years, with statutory year-end rules. Customs files: normally 6 years from the end of the customs-decision year. Unclaimed instant quotes: 30 days; unconverted account quotes: normally 24 months. KYC records: configured policy normally 5 years after the end of the relationship year; AML applicability must be assessed. Legal holds suspend deletion. The responsible owner reviews retention runs and any data without an automated deletion rule; contact us for the period applicable to your records."
        }
      ]
    },
    {
      "id": "rights",
      "title": "Your rights",
      "blocks": [
        {
          "p": "You have the right to access your data, to have it corrected or erased, to restrict or object to its processing (including objecting to direct marketing at any time), to data portability, and to withdraw consent where processing is based on consent."
        },
        {
          "p": "Send requests to [Privacy contact](/company#contact). We answer within one month. Data we must keep by law, such as customs and accounting records, cannot be erased before the retention period ends."
        },
        {
          "p": "You may lodge a complaint with the Office of the Data Protection Ombudsman (Tietosuojavaltuutetun toimisto), [tietosuoja.fi](https://tietosuoja.fi)."
        }
      ]
    },
    {
      "id": "cookies",
      "title": "Cookies and local storage",
      "blocks": [
        {
          "p": "See the [Cookie and browser storage notice](/cookies) for names, purposes, lifetimes and removal instructions."
        }
      ]
    },
    {
      "id": "security",
      "title": "Security",
      "blocks": [
        {
          "p": "Access is limited to authorised owners, contracted service providers and software roles that need the data, using role-based permissions and audit records. Passwords are salted hashes; traffic is encrypted in transit."
        }
      ]
    },
    {
      "id": "changes",
      "title": "Changes to this notice",
      "blocks": [
        {
          "p": "We update this notice when our processing changes. The version and date are shown at the top of this page."
        }
      ]
    }
  ],
  "fi": [
    {
      "id": "controller",
      "title": "Rekisterinpitäjä ja yhteystiedot",
      "blocks": [
        {
          "p": "Palveluntarjoaja on rekisterinpitäjä. Virallinen nimi, rekisteröinnin tila, postiosoite ja tietosuojan yhteystiedot julkaistaan [Yritystiedot](/company#contact)-sivulla. Rekisteröinti on kesken, kunnes se on siellä nimenomaisesti vahvistettu; operaattorin on yksilöitävä vastuullinen rekisterinpitäjä ennen tuotantoasiakkaiden tietojen keräämistä."
        }
      ]
    },
    {
      "id": "data",
      "title": "Keiden tietoja käsittelemme ja mitä",
      "blocks": [
        {
          "ul": [
            "Asiakkaidemme yhteyshenkilöt ja portaalin käyttäjät: nimi, sähköposti, puhelin, rooli, yritys, kieli sekä kirjautumis- ja turvallisuustiedot (salasanan tiiviste, kirjautumisajat).",
            "Lähetysten osapuolet: lähettäjien, vastaanottajien, ilmoitusosapuolten sekä nouto- ja toimitusyhteyshenkilöiden nimet, osoitteet ja yhteystiedot.",
            "Lataamanne tulli- ja kauppa-asiakirjat, kuten kauppalaskut, pakkausluettelot, kuljetusasiakirjat ja valtakirjat. Niissä voi olla yhteyshenkilöiden ja elinkeinonharjoittajien henkilötietoja.",
            "Asiakkaan tunteminen ja pakotetarkistukset: yrityksen edustajien nimet ja tarkistusten tulokset.",
            "Maksut: laskujen ja maksujen tila. Korttimaksut käsittelee Stripe; emme näe emmekä tallenna kokonaisia korttinumeroita.",
            "Portaalin viestit ja sähköpostit kanssamme.",
            "Verkkosivujen ja hinta heti -työkalun käyttäjät: IP-osoite (pyyntömäärien rajoittamiseen ja palvelun suojaamiseen), syöttämänne reitti ja tavara sekä palvelinlokien tekniset tiedot. Hinta heti -tarjoukset tallennetaan ilman nimeänne, kunnes liitätte ne tiliin."
          ]
        }
      ]
    },
    {
      "id": "purposes",
      "title": "Käsittelyn tarkoitukset ja oikeusperusteet",
      "blocks": [
        {
          "table": {
            "head": [
              "Tarkoitus",
              "Oikeusperuste (tietosuoja-asetuksen 6 artiklan 1 kohta)"
            ],
            "rows": [
              [
                "Tarjoukset, varaukset, kuljetus- ja tullauspalvelut sekä asiakastili",
                "Sopimus (b); yritysasiakkaiden yhteyshenkilöiden osalta oikeutettu etu (f)"
              ],
              [
                "Tulli-ilmoitukset ja tullikirjanpidon säilyttäminen",
                "Lakisääteinen velvoite (c): EU:n tullilainsäädäntö ja tullilaki"
              ],
              [
                "Kirjanpito ja laskutus",
                "Lakisääteinen velvoite (c): kirjanpitolaki (1336/1997)"
              ],
              [
                "Asiakkaan tunteminen ja pakotetarkistukset",
                "Lakisääteinen velvoite (c): EU:n pakoteasetukset; oikeutettu etu (f) petosten ja laittomien lähetysten estämisessä"
              ],
              [
                "Tietoturva, väärinkäytösten estäminen ja hinta heti -työkalun pyyntörajoitus",
                "Oikeutettu etu (f)"
              ],
              [
                "Palveluviestit: varausvahvistukset, seurantapäivitykset, laskut",
                "Sopimus (b)"
              ],
              [
                "Uutiset ja tarjoukset yritysten yhteyshenkilöille",
                "Oikeutettu etu (f), jokaisessa viestissä mahdollisuus kieltäytyä; suostumus (a), kun laki sitä edellyttää"
              ],
              [
                "Reklamaatioiden ja oikeudellisten riitojen hoitaminen",
                "Oikeutettu etu (f)"
              ]
            ]
          }
        }
      ]
    },
    {
      "id": "ai",
      "title": "Tekoälyavusteinen asiakirjojen käsittely",
      "blocks": [
        {
          "p": "Käytämme tekoälymallin tarjoajaa lataamienne asiakirjojen (esim. kauppalaskut ja pakkausluettelot) lukemiseen, tietojen poimimiseen ja tullinimike-ehdotusten tekemiseen. Kun Autopilot ei ole käytössä, valtuutettu omistaja tai palveluntarjoaja hyväksyy asiakasvastaukset ja tullauksen. Operaattorin kytkemä Autopilot voi lähettää vastauksia, hyväksyä yritystarjouksia rajoissa, välittää tarkistetut tulli-ilmoitukset määritettyä kanavaa pitkin, ratkaista poikkeamia ja laskuttaa. Toimet kirjataan, ja operaattori voi poistaa automaation käytöstä."
        },
        {
          "p": "Toimintaperiaatteet perustuvat lähetystietoihin, hinta- ja katerajoihin, sallittuihin reitteihin ja pakollisiin tarkistuksiin. Emme pisteytä luonnollisia henkilöitä luottoa tai työsuhdetta varten. Voitte pyytää vastuulliselta omistajalta ihmisen arviota, selitystä ja virheen korjaamista automaattisesta toimesta. Tietosuoja-asetuksen 22 artiklan oikeudet säilyvät. Tarjoaja käsittelee asiakirjoja puolestamme henkilötietojen käsittelijänä 28 artiklan mukaisesti."
        },
        {
          "p": "Jos luonnollista henkilöä koskevalla päätöksellä on oikeudellisia tai vastaavia merkittäviä vaikutuksia, 22 artiklan poikkeus ja suojatoimet on osoitettava tai järjestettävä aito ihmisen arvio ennen päätöstä. Toiminimet ja henkilötakaukset edellyttävät tätä arviota; yritystilin nimi ei poista yksilön oikeuksia. Sivuston käyttö ei ole yleissuostumus tällaisiin päätöksiin."
        }
      ]
    },
    {
      "id": "partner-sourcing",
      "title": "Kumppanien hankinta (Hankinta-autopilotti)",
      "blocks": [
        {
          "p": "Etsiäksemme kuljetusyhtiöitä, kuljetusliikkeitä ja agentteja sekä pyytääksemme rahtitarjouksia käsittelemme yritysten itse julkaisemia yritysyhteystietoja: yrityksen nimi, Y-tunnus, verkkosivu ja yrityksen omalla sivulla näkyvä sähköpostiosoite (ensisijaisesti rooliosoite, kuten myynti@). Lähteet ovat julkiset rekisterit (kaupparekisteri / YTJ:n avoin data), Wikidata ja yritysten verkkosivut; tallennamme jokaisen yhteystiedon lähdeosoitteen."
        },
        {
          "p": "Oikeusperuste: oikeutettu etumme hankkia kuljetuksia asiakkaillemme (tietosuoja-asetuksen 6 artiklan 1 kohdan f alakohta). Lähestymme vain oikeushenkilöitä, emme yksityisiä elinkeinonharjoittajia tai yksityishenkilöiden osoitteita, ja lähetämme rajallisen määrän viestejä päivässä. Jokainen viesti kertoo lähettäjäksi Huolitsija Oy:n, mistä osoite löytyi, ja sisältää linkin, jolla viestit loppuvat yhdellä klikkauksella; myös vastaus, jossa pyydätte lopettamaan, riittää. Säilytämme kieltolistan, jotta emme ota uudelleen yhteyttä."
        },
        {
          "p": "Säilytämme yhteys- ja viestitiedot 24 kuukautta viimeisestä yhteydenotosta ja hyväksytyt hinnat lähteineen kirjanpito- ja tullisääntöjen edellyttämän ajan. Vastaukset lukee ohjelmisto, mukaan lukien käsittelijänämme toimiva tekoälymallin tarjoaja. Voitte vastustaa käsittelyä tai pyytää tietojen tarkastamista tai poistamista osoitteesta [Tietosuojan yhteystiedot](/company#contact)."
        }
      ]
    },
    {
      "id": "recipients",
      "title": "Tietojen vastaanottajat",
      "blocks": [
        {
          "p": "Julkaistu palvelu käyttää Emergent-palvelualustaa ja MongoDB-tietokantaa, OpenAI:ta asiakirjojen ja viestien automaattiseen käsittelyyn sekä Stripeä maksamiseen. Stripe voi toimia myös itsenäisenä rekisterinpitäjänä omissa lakisääteisissä ja petostentorjunnan tarkoituksissaan. MongoDB on tässä tietokantaohjelmisto, ei näyttö erillisestä MongoDB Atlas -sopimuksesta."
        },
        {
          "table": {
            "head": [
              "Palvelu",
              "Tiedot / käyttöönotto"
            ],
            "rows": [
              [
                "Emergent",
                "Sovellus, tietokanta, asiakirjat ja tekniset lokit; palvelualustan käsittelijä."
              ],
              [
                "OpenAI",
                "Valitut viestit ja asiakirjat poimintaa, vastauksia ja operatiivisia päätöksiä varten; tekoälykäsittelijä."
              ],
              [
                "Stripe",
                "Maksajan yhteystiedot, laskuviite, summa ja maksusivun tiedot. Testimaksut erotetaan oikeista maksuista."
              ],
              [
                "Postmark / Resend",
                "Sähköpostiosoitteet, viestit ja toimitustapahtumat vain valitun välityspalvelun ollessa käytössä. Lokitila ei lähetä sähköpostia."
              ],
              [
                "Twilio",
                "Puhelinnumero, tilatut palvelutekstiviestit ja toimitustila vain SMS-välityksen ollessa käytössä. Lokitila ei lähetä tekstiviestejä."
              ],
              [
                "Redkik",
                "Lähetys-, tavaran arvo- ja vakuutuksen yhteystiedot vain, jos oikea vakuutuspalvelu on otettu käyttöön ja vakuutus pyydetty; roolit määräytyvät vakuutussopimuksessa."
              ],
              [
                "OpenSanctions",
                "Nimet PEP-tarkistukseen vain maksullisen rajapinnan ollessa käytössä. Julkiset rekisterihaut käyttävät myös PRH/YTJ:tä, VIES/EORI:a, GLEIF:iä ja Wikidataa."
              ]
            ]
          }
        },
        {
          "p": "Kuljetusyhtiöt, varastot, terminaalit, tulli-, vero- ja vakuutustahot saavat vain lähetyksen tai lakisääteisten tehtäviensä edellyttämät tiedot ja voivat olla itsenäisiä rekisterinpitäjiä. Valinnainen integraatio ei osoita tilin hyväksyntää tai vakuutusturvaa. Ajantasaisen vastaanottajaluettelon, sopimusosapuolet ja suojatoimet saa tietosuojan yhteyshenkilöltä."
        },
        {
          "p": "Karttapohjat haetaan OpenFreeMap-palvelusta. Oletuksena määritetyillä geokoodaus- ja reitityspalveluilla osoite- tai koordinaattihakuja lähetetään OpenStreetMap Nominatim-, Photon (komoot)- ja OSRM-palveluihin. Ne saavat vastauksen tuottamiseen tarvittavat tekniset pyyntötiedot; selaimen karttapyynnöt välittävät myös kävijän verkko-osoitteen. Määritetty rajapinta ja sen ehdot määräävät kunkin haun vastaanottajan."
        }
      ]
    },
    {
      "id": "transfers",
      "title": "Siirrot EU:n ja ETA:n ulkopuolelle",
      "blocks": [
        {
          "p": "Kuljetus EU:n ja ETA:n ulkopuoliseen maahan tai sieltä edellyttää lähetystietojen luovuttamista kyseisen maan kuljetusyhtiöille, agenteille ja viranomaisille. Luovutamme vain sen, mitä tilaamanne kuljetuksen suorittaminen vaatii."
        },
        {
          "p": "Kun käsittelijä käsittelee tietoja EU:n ja ETA:n ulkopuolella, siirto perustuu Euroopan komission tietosuojan riittävyyttä koskevaan päätökseen tai komission vakiosopimuslausekkeisiin."
        },
        {
          "p": "Tallennusalueet ja käsittelijöiden edelleen siirrot on vahvistettava sopimuksista. Palveluntarjoajan nimi ei takaa käsittelyä vain EU:ssa. Sovellettavien suojatoimien tiedot tai jäljennökset saa tietosuojan yhteyshenkilöltä."
        }
      ]
    },
    {
      "id": "retention",
      "title": "Säilytysajat",
      "blocks": [
        {
          "p": "Säilytysaika riippuu tarkoituksesta, asiakassuhteen päättymisestä ja lakisääteisistä velvoitteista. Kirjanpitokirjat ja tilinpäätökset: vähintään 10 vuotta; tositteet: vähintään 6 vuotta lain vuodenloppusääntöjen mukaan. Tulliaineisto: yleensä 6 vuotta tullipäätösvuoden lopusta. Lunastamattomat pikatarjoukset: 30 päivää; varaamattomat tilitarjoukset: yleensä 24 kuukautta. KYC-tiedot: asetettu käytäntö yleensä 5 vuotta asiakassuhteen päättymisvuoden lopusta; rahanpesulain soveltuvuus arvioidaan erikseen. Oikeudellinen säilytysvelvoite keskeyttää poistamisen. Vastuullinen omistaja tarkistaa säilytysajot ja aineiston, jolle ei ole automaattista poistosääntöä. Pyytäkää omiin tietoihinne soveltuva aika yhteyshenkilöltä."
        }
      ]
    },
    {
      "id": "rights",
      "title": "Oikeutenne",
      "blocks": [
        {
          "p": "Teillä on oikeus saada pääsy tietoihinne, oikaista tai poistattaa ne, rajoittaa niiden käsittelyä tai vastustaa sitä (myös suoramarkkinointia milloin tahansa), siirtää tiedot järjestelmästä toiseen sekä peruuttaa suostumuksenne, kun käsittely perustuu suostumukseen."
        },
        {
          "p": "Lähettäkää pyynnöt osoitteeseen [Tietosuojan yhteystiedot](/company#contact). Vastaamme kuukauden kuluessa. Lain nojalla säilytettäviä tietoja, kuten tulli- ja kirjanpitoaineistoa, ei voi poistaa ennen säilytysajan päättymistä."
        },
        {
          "p": "Voitte tehdä valituksen tietosuojavaltuutetun toimistolle: [tietosuoja.fi](https://tietosuoja.fi)."
        }
      ]
    },
    {
      "id": "cookies",
      "title": "Evästeet ja selaimen tallennustila",
      "blocks": [
        {
          "p": "Katso nimet, käyttötarkoitukset, kestot ja poistamisohjeet [eväste- ja selaintallennusilmoituksesta](/cookies)."
        }
      ]
    },
    {
      "id": "security",
      "title": "Tietoturva",
      "blocks": [
        {
          "p": "Tietoja saavat käsitellä vain niitä tarvitsevat valtuutetut omistajat, sopimuspalveluntarjoajat ja ohjelmistoroolit roolipohjaisin oikeuksin ja lokitiedoin. Salasanat tallennetaan suolattuina tiivisteinä; tietoliikenne salataan."
        }
      ]
    },
    {
      "id": "changes",
      "title": "Muutokset tähän selosteeseen",
      "blocks": [
        {
          "p": "Päivitämme selostetta, kun käsittelymme muuttuu. Versio ja päivämäärä näkyvät sivun yläosassa."
        }
      ]
    }
  ]
};
