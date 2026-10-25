"""Explicit ICS2 sandbox: simulated authority responses, real validated IE3 XML."""
from services.customs_messages import ics2
from services.tulli_sandbox import sandbox_mrn


def response(filing, message):
    mrn=filing.get('mrn') or sandbox_mrn()
    stamp={'DateTime':ics2.timestamp()}
    data={'MRN':mrn}
    if message=='IE3R01':
        data.update(LRN=filing['lrn'],registrationDate=stamp,
                    SpecificCircumstanceIndicator=filing['filing_type'][3:])
    elif message=='IE3N03':
        data.update(completionDate=stamp,responsibleMemberState={'country':'FI'})
    elif message=='IE3Q01':
        data.update(documentIssueDate=stamp,responsibleMemberState={'country':'FI'},doNotLoadDetails={})
    elif message=='IE3Q02':
        data.update(documentIssueDate=stamp,responsibleMemberState={'country':'FI'},
            declarant={'identificationNumber':filing['eori']},
            referralRequestDetails={'referralRequestReference':'SANDBOX-RFI','requestType':'RFI'})
    elif message=='IE3R07':
        data.update(completionDate=stamp,declarant={'identificationNumber':filing['eori']})
    else: raise ValueError('Unsupported sandbox response')
    return ics2.build(message,data)
