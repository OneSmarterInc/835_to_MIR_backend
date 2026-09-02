"""Default MIR mapping configuration.

These defaults reproduce the current code-driven converter.  The admin mapping
screen can persist overrides without changing Python source.
"""
from __future__ import annotations

from copy import deepcopy


def _field(section, field_id, target, name, type_, length, start, scope,
           map_type, mapping, desc, **opts):
    return {
        "section": section,
        "id": field_id,
        "target": target,
        "name": name,
        "type": type_,
        "length": length,
        "start": start,
        "end": start + length - 1,
        "scope": scope,
        "mapType": map_type,
        "map": mapping,
        "desc": desc,
        "technicalRule": opts.get("technicalRule", ""),
        "sourceExplanation": opts.get("sourceExplanation", ""),
        "upper": opts.get("upper", False),
        "trim": opts.get("trim", True),
        "truncate": opts.get("truncate", True),
        "align": opts.get("align", "left"),
        "pad": opts.get("pad", " "),
        "fallbackType": opts.get("fallbackType", "Blank"),
        "fallbackValue": opts.get("fallbackValue", ""),
        "sampleIn": opts.get("sampleIn", ""),
        "sampleOut": opts.get("sampleOut", ""),
    }


F = []
a = F.append

# Claim/header fields — baseline converter behavior.
a(_field('Claim Header','MIR000','record_type','MPL Record Type','A',2,1,'Claim','Hardcoded Text','MO','Outgoing MIR record type. Current converter writes MO.',sampleOut='MO'))
a(_field('Claim Header','MIR100','claim_number','Claim ICN / Claim Number','A',17,3,'Claim','Direct from 835','CLP01','Claim identifier carried in the fixed MIR claim header.',upper=True,sampleIn='CLP*86520262090402400*4*350*0**ZZ*QYP144*11*1',sampleOut='86520262090402400'))
a(_field('Claim Header','HEADER 20–25','claim_reference','Claim Cross Reference','A',6,20,'Claim','Direct from 835','CLP07','Cross-reference value currently mapped from CLP07 and truncated to six characters.',upper=True,sampleIn='CLP*...*ZZ*QYP144*11*1',sampleOut='QYP144'))
a(_field('Claim Header','MIR200','api_date_1','TPA / Process Date 1','D',8,37,'Claim','System / Runtime','PROCESS_DATE','First 8-character process-date field. Uses the date stored for the conversion run so regeneration is reproducible.',sampleOut='20260814'))
a(_field('Claim Header','MIR201','api_date_2','TPA / Process Date 2','D',8,45,'Claim','System / Runtime','PROCESS_DATE','Second 8-character process-date field. Reuses the same stored run date as MIR200.',sampleOut='20260814'))
a(_field('Claim Header','MIR202','claim_status','Claim Disposition','A',1,53,'Claim','Formula','Derived claim disposition','Derives MIR disposition from CLP payment status and fully denied service lines.',technicalRule='CLAIM_DISPOSITION()',upper=True,sampleIn='CLP02 plus SVC/CAS adjudication',sampleOut='4'))
a(_field('Claim Header','MIR203/204','primary_reason','Primary Claim Reason','A',5,55,'Claim','Formula','First applicable CAS Group + Reason','Gets the claim reason from the CAS adjustment information.',technicalRule='CLAIM_PRIMARY_REASON()',sourceExplanation='CAS01 = adjustment group (for example PR or CO). Reason comes from CAS02/CAS05/CAS08/CAS11/CAS14/CAS17. The current rule chooses the first applicable adjustment reason.',upper=True,sampleIn='CLP02=4; first service CAS*PR*227*280',sampleOut='PR227'))
a(_field('Subscriber','MIR301','member_id','Subscriber / Member ID','A',12,60,'Claim','Direct from 835','NM1[IL,MI].NM109','Subscriber identifier. Current converter prefers NM1*IL with NM108=MI and takes NM109.',upper=True,sampleIn='NM1*IL*1*RIEGNER*CORBIN*C***MI*J5YBD0000042',sampleOut='J5YBD000004'))
a(_field('Subscriber','MIR302','group_number','Group Number','A',8,77,'Claim','Direct from 835','REF[1L].REF02','Subscriber group number.',upper=True,fallbackType='Hardcoded',fallbackValue='99999999',sampleIn='REF*1L*10670170',sampleOut='10670170'))
a(_field('Patient','MIR401','patient_last_name','Patient Last Name','A',20,86,'Claim','Direct from 835','NM1[QC].NM103','Patient surname from the QC entity name segment.',upper=True,sampleIn='NM1*QC*1*RIEGNER*JENNA*L',sampleOut='RIEGNER'))
a(_field('Patient','MIR402','patient_first_name','Patient First Name','A',10,106,'Claim','Direct from 835','NM1[QC].NM104','Patient first name from the QC entity name segment.',upper=True,sampleIn='NM1*QC*1*RIEGNER*JENNA*L',sampleOut='JENNA'))
a(_field('Patient','MIR403','patient_middle_initial','Patient Middle Initial','A',1,116,'Claim','Direct from 835','NM1[QC].NM105','Patient middle name/initial; current converter truncates to one character.',upper=True,sampleIn='NM1*QC*1*RIEGNER*JENNA*L',sampleOut='L'))
a(_field('Patient','MIR404','patient_sex','Patient Sex','A',1,117,'Claim','Blank','','Patient sex is not currently populated by the 835 parser; field remains blank.',sampleOut=''))
a(_field('Patient','MIR405','dob','Patient Date of Birth','D',8,118,'Claim','Direct from 835','DTM[036].DTM02','Patient date of birth in CCYYMMDD.',sampleIn='DTM*036*19900712',sampleOut='19900712'))
a(_field('TPA Processing Data','MIR501','process_indicator','Process Code Indicator','A',1,126,'Claim','Formula','Derived adjustment indicator','Returns A for streamline facility type AA and replacement/void claim-frequency codes.',technicalRule='PROCESS_INDICATOR()',upper=True,sampleOut='A'))

for i, start in enumerate([131,142,153,164,175,186,197], 1):
    a(_field('Claim Amount / Default Areas',f'MIR500-series {i}',f'claim_numeric_{i}',f'Claim Numeric Field {i}','N',11,start,'Claim','Hardcoded Text','0000000000+','Current converter preserves this MIR numeric area using the signed-zero default.',align='right',sampleOut='0000000000+'))
a(_field('Claim Amount / Default Areas','MIR500-series 8','claim_numeric_8','Claim Numeric Field 8','N',11,233,'Claim','Hardcoded Text','0000000000+','Current converter preserves this MIR numeric area using the signed-zero default.',align='right',sampleOut='0000000000+'))
a(_field('Record Control','MIR912','record_sequence','Record Sequence Number','N',2,249,'Physical record','System / Runtime','RECORD_SEQUENCE','Physical MIR record sequence for claims split across more than 50 service lines.',align='right',sampleOut='01'))
a(_field('Record Control','MIR913','max_record_sequence','Maximum Record Number','N',2,251,'Physical record','System / Runtime','MAX_RECORD_SEQUENCE','Highest sequence number for this claim; repeated on every physical record.',align='right',sampleOut='01'))
a(_field('Record Control','HEADER 333–334','service_count','Number of Service Lines','N',2,333,'Physical record','System / Runtime','SERVICE_COUNT','Number of service blocks written in this physical MIR record.',align='right',sampleOut='01'))

# Service block positions are relative to the beginning of each 303-byte service block.
a(_field('Service Line','MIR1004','status','Line Disposition','A',1,16,'Service','Formula','Claim Status (CLP02) + Paid Amount (SVC03) + CAS adjustments','Uses the same line-status logic as the current converter.',technicalRule='SERVICE_STATUS()',sourceExplanation='CLP02 = claim status. SVC03 = paid amount for the service line. CAS = adjustment information for the service line. These values are used together to determine the MIR line disposition.',upper=True,sampleIn='CLP02=1; SVC03=0; CAS*PR*227*280',sampleOut='4'))
a(_field('Service Line','MIR1005/1006','service_primary_reason','Line Primary Reason','A',5,18,'Service','Formula','Applicable CAS Group + Reason for this service line','Gets the service-line reason from the CAS adjustment information.',technicalRule='SERVICE_REASON()',sourceExplanation='CAS01 = adjustment group. Reason comes from CAS02/CAS05/CAS08/CAS11/CAS14/CAS17. CLP02 and SVC03 are also considered by the current line-reason logic.',upper=True,sampleIn='CAS*PR*227*280',sampleOut='PR227'))
a(_field('Service Line','Service Units','service_units','Number of Services','N',6,23,'Service','Direct from 835','SVC05','SVC05 converted to a 5-digit signed count with trailing sign.',align='right',sampleIn='SVC*HC:99214*275*109.3**1',sampleOut='00001+'))
a(_field('Service Line','Approved Units','approved_units','Approved Units','N',6,29,'Service','Hardcoded Text','00000+','Current converter uses MIR signed-zero count default.',align='right',sampleOut='00000+'))
a(_field('Service Line','Service Small Count','small_count','Small Count / Default','N',5,35,'Service','Hardcoded Text','0000+','Current converter uses MIR signed-zero count default.',align='right',sampleOut='0000+'))
a(_field('Service Line','Line Numeric 1','line_numeric_1','Line Numeric Field 1','N',11,40,'Service','Hardcoded Text','0000000000+','Current converter signed-zero default.',align='right',sampleOut='0000000000+'))
a(_field('Service Line','Service Charge','service_charge','Submitted Service Charge','N',11,51,'Service','Direct from 835','SVC02','SVC02 formatted as implied 2-decimal amount with trailing sign.',align='right',sampleIn='SVC*HC:99214*275*109.3**1',sampleOut='0000027500+'))
a(_field('Service Line','Line Numeric 2','line_numeric_2','Line Numeric Field 2','N',11,62,'Service','Hardcoded Text','0000000000+','Current converter signed-zero default.',align='right',sampleOut='0000000000+'))
a(_field('Service Line','Line Numeric 3','line_numeric_3','Line Numeric Field 3','N',11,73,'Service','Hardcoded Text','0000000000+','Current converter signed-zero default.',align='right',sampleOut='0000000000+'))
a(_field('Service Line','Prepriced / Covered','covered_charge','Covered / Pre-Priced Amount','N',11,84,'Service','Formula','Derived pre-priced amount','Uses full service charge for denied lines; otherwise charge less contractual reductions.',technicalRule='PREPRICED_AMOUNT()',align='right',sampleOut='0000013930+'))
a(_field('Service Line','MIR1018','paid_amount','TPA Approved-to-Pay Amount','N',11,95,'Service','Direct from 835','SVC03','Current converter maps SVC03 paid amount and formats with implied 2 decimals + trailing sign.',align='right',sampleIn='SVC*HC:99214*275*109.3**1',sampleOut='0000010930+'))
a(_field('Service Line','MIR1019','patient_liability','Patient Liability','N',11,106,'Service','Formula','Derived patient liability','Uses full service charge for denied lines; otherwise covered amount less paid amount.',technicalRule='MIR_PATIENT_LIABILITY()',align='right',sampleOut='0000003000+'))
a(_field('Service Line','Service Small Numeric 2','small_numeric_2','Small Numeric / Default','N',5,117,'Service','Hardcoded Text','0000+','Current converter signed-zero small-number default.',align='right',sampleOut='0000+'))
a(_field('Service Line','Line Numeric 4','line_numeric_4','Line Numeric Field 4','N',11,122,'Service','Hardcoded Text','0000000000+','Current converter signed-zero default.',align='right',sampleOut='0000000000+'))
a(_field('Service Line','Line Numeric 5','line_numeric_5','Line Numeric Field 5','N',11,133,'Service','Hardcoded Text','0000000000+','Current converter signed-zero default.',align='right',sampleOut='0000000000+'))

for slot in range(1, 11):
    rs = 144 + (slot - 1) * 16
    amt_start = 149 + (slot - 1) * 16
    a(_field('Payment Reductions',f'MIR1100–1130 · R{slot}',f'reduction_{slot}_reason',f'Payment Reduction {slot} Reason','A',5,rs,'Service','Formula',f'Payment reduction reason - Slot {slot}','Maps ordinary PR1/PR2/PR3 and the MPL PR45-to-PR119 rule.',technicalRule=f'REDUCTION_REASON({slot})',upper=True,sampleIn='CAS adjustments',sampleOut=''))
    a(_field('Payment Reductions',f'MIR1100–1130 · A{slot}',f'reduction_{slot}_amount',f'Payment Reduction {slot} Amount','N',11,amt_start,'Service','Formula',f'Payment reduction amount - Slot {slot}','Uses the amount paired with the mapped reduction reason.',technicalRule=f'REDUCTION_AMOUNT({slot})',align='right',sampleIn='CAS adjustments',sampleOut='0000000000+'))

DEFAULT_MAPPINGS = F


def defaults():
    return deepcopy(DEFAULT_MAPPINGS)
