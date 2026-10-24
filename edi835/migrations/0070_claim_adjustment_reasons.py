"""Seed the user-supplied CAS reasons, including historical/stopped codes.

Descriptions and source dates/notes are preserved verbatim. Historical codes
remain resolvable when viewing older remittances.
"""
from django.db import migrations, models

REASONS = [('1', 'Deductible Amount', 'Start: 01/01/1995'),
 ('2', 'Coinsurance Amount', 'Start: 01/01/1995'),
 ('3', 'Co-payment Amount', 'Start: 01/01/1995'),
 ('4',
  'The procedure code is inconsistent with the modifier used. Usage: Refer to the 835 Healthcare Policy '
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 03/01/2020'),
 ('5',
  'The procedure code/type of bill is inconsistent with the place of service. Usage: Refer to the 835 '
  'Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 03/01/2018'),
 ('6',
  "The procedure/revenue code is inconsistent with the patient's age. Usage: Refer to the 835 Healthcare "
  'Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('7',
  "The procedure/revenue code is inconsistent with the patient's gender. Usage: Refer to the 835 Healthcare "
  'Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('8',
  'The procedure code is inconsistent with the provider type/specialty (taxonomy). Usage: Refer to the 835 '
  'Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('9',
  "The diagnosis is inconsistent with the patient's age. Usage: Refer to the 835 Healthcare Policy "
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('10',
  "The diagnosis is inconsistent with the patient's gender. Usage: Refer to the 835 Healthcare Policy "
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('11',
  'The diagnosis is inconsistent with the procedure. Usage: Refer to the 835 Healthcare Policy '
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('12',
  'The diagnosis is inconsistent with the provider type. Usage: Refer to the 835 Healthcare Policy '
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('13', 'The date of death precedes the date of service.', 'Start: 01/01/1995'),
 ('14', 'The date of birth follows the date of service.', 'Start: 01/01/1995'),
 ('15',
  'The authorization number is missing, invalid, or does not apply to the billed services or provider.',
  'Start: 01/01/1995 | Last Modified: 11/01/2017 | Stop: 05/01/2018'),
 ('16',
  'Claim/service lacks information or has submission/billing error(s). Usage: Do not use this code for '
  'claims attachment(s)/other documentation. At least one Remark Code must be provided (may be comprised of '
  'either the NCPDP Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT.) Refer to the '
  '835 Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 03/01/2018'),
 ('17',
  'Requested information was not provided or was insufficient/incomplete. At least one Remark Code must be '
  'provided (may be comprised of either the Remittance Advice Remark Code or NCPDP Reject Reason Code.)',
  'Start: 01/01/1995 | Last Modified: 09/21/2008 | Stop: 07/01/2009'),
 ('18',
  "Exact duplicate claim/service (Use only with Group Code OA except where state workers' compensation "
  'regulations requires CO)',
  'Start: 01/01/1995 | Last Modified: 06/02/2013'),
 ('19',
  "This is a work-related injury/illness and thus the liability of the Worker's Compensation Carrier.",
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('20',
  'This injury/illness is covered by the liability carrier.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('21',
  'This injury/illness is the liability of the no-fault carrier.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('22',
  'This care may be covered by another payer per coordination of benefits.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('23',
  'The impact of prior payer(s) adjudication including payments and/or adjustments. (Use only with Group '
  'Code OA)',
  'Start: 01/01/1995 | Last Modified: 09/30/2012'),
 ('24',
  'Charges are covered under a capitation agreement/managed care plan.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('25',
  'Payment denied. Your Stop loss deductible has not been met.',
  'Start: 01/01/1995 | Stop: 04/01/2008'),
 ('26', 'Expenses incurred prior to coverage.', 'Start: 01/01/1995'),
 ('27', 'Expenses incurred after coverage terminated.', 'Start: 01/01/1995'),
 ('28',
  'Coverage not in effect at the time the service was provided.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Redundant to codes 26&27.'),
 ('29', 'The time limit for filing has expired.', 'Start: 01/01/1995'),
 ('30',
  'Payment adjusted because the patient has not met the required eligibility, spend down, waiting, or '
  'residency requirements.',
  'Start: 01/01/1995 | Stop: 02/01/2006'),
 ('31', 'Patient cannot be identified as our insured.', 'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('32',
  'Our records indicate the patient is not an eligible dependent.',
  'Start: 01/01/1995 | Last Modified: 03/01/2018'),
 ('33', 'Insured has no dependent coverage.', 'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('34', 'Insured has no coverage for newborns.', 'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('35', 'Lifetime benefit maximum has been reached.', 'Start: 01/01/1995 | Last Modified: 10/31/2002'),
 ('36', 'Balance does not exceed co-payment amount.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('37', 'Balance does not exceed deductible.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('38',
  'Services not provided or authorized by designated (network/primary care) providers.',
  'Start: 01/01/1995 | Last Modified: 06/02/2013 | Stop: 01/01/2013\n'
  'Notes: CARC codes 242 and 243 are replacements for this deactivated code'),
 ('39', 'Services denied at the time authorization/pre-certification was requested.', 'Start: 01/01/1995'),
 ('40',
  'Charges do not meet qualifications for emergent/urgent care. Usage: Refer to the 835 Healthcare Policy '
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('41', 'Discount agreed to in Preferred Provider contract.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('42',
  'Charges exceed our fee schedule or maximum allowable amount. (Use CARC 45)',
  'Start: 01/01/1995 | Last Modified: 10/31/2006 | Stop: 06/01/2007'),
 ('43', 'Gramm-Rudman reduction.', 'Start: 01/01/1995 | Stop: 07/01/2006'),
 ('44', 'Prompt-pay discount.', 'Start: 01/01/1995'),
 ('45',
  'Charge exceeds fee schedule/maximum allowable or contracted/legislated fee arrangement. Usage: This '
  'adjustment amount cannot equal the total service or claim charge amount; and must not duplicate provider '
  'adjustment amounts (payments and contractual reductions) that have resulted from prior payer(s) '
  'adjudication. (Use only with Group Codes PR or CO depending upon liability)',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('46',
  'This (these) service(s) is (are) not covered.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 96.'),
 ('47',
  'This (these) diagnosis(es) is (are) not covered, missing, or are invalid.',
  'Start: 01/01/1995 | Stop: 02/01/2006'),
 ('48',
  'This (these) procedure(s) is (are) not covered.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 96.'),
 ('49',
  'This is a non-covered service because it is a routine/preventive exam or a diagnostic/screening procedure '
  'done in conjunction with a routine/preventive exam. Usage: Refer to the 835 Healthcare Policy '
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('50',
  "These are non-covered services because this is not deemed a 'medical necessity' by the payer. Usage: "
  'Refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if '
  'present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('51',
  'These are non-covered services because this is a pre-existing condition. Usage: Refer to the 835 '
  'Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('52',
  'The referring/prescribing/rendering provider is not eligible to refer/prescribe/order/perform the service '
  'billed.',
  'Start: 01/01/1995 | Stop: 02/01/2006'),
 ('53',
  'Services by an immediate relative or a member of the same household are not covered.',
  'Start: 01/01/1995'),
 ('54',
  'Multiple physicians/assistants are not covered in this case. Usage: Refer to the 835 Healthcare Policy '
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('55',
  'Procedure/treatment/drug is deemed experimental/investigational by the payer. Usage: Refer to the 835 '
  'Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('56',
  "Procedure/treatment has not been deemed 'proven to be effective' by the payer. Usage: Refer to the 835 "
  'Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('57',
  'Payment denied/reduced because the payer deems the information submitted does not support this level of '
  "service, this many services, this length of service, this dosage, or this day's supply.",
  'Start: 01/01/1995 | Stop: 06/30/2007\nNotes: Split into codes 150, 151, 152, 153 and 154.'),
 ('58',
  'Treatment was deemed by the payer to have been rendered in an inappropriate or invalid place of service. '
  'Usage: Refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment Information '
  'REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('59',
  'Processed based on multiple or concurrent procedure rules. (For example multiple surgery or diagnostic '
  'imaging, concurrent anesthesia.) Usage: Refer to the 835 Healthcare Policy Identification Segment (loop '
  '2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('60',
  'Charges for outpatient services are not covered when performed within a period of time prior to or after '
  'inpatient services.',
  'Start: 01/01/1995 | Last Modified: 06/01/2008'),
 ('61',
  'Adjusted for failure to obtain second surgical opinion',
  'Start: 01/01/1995 | Last Modified: 03/01/2017\n'
  'Notes: The description effective date was inadvertently published as 3/1/2016 on 7/1/2016. That has been '
  'corrected to 1/1/2017.'),
 ('62',
  'Payment denied/reduced for absence of, or exceeded, pre-certification/authorization.',
  'Start: 01/01/1995 | Last Modified: 10/31/2006 | Stop: 04/01/2007'),
 ('63', 'Correction to a prior claim.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('64', 'Denial reversed per Medical Review.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('65',
  'Procedure code was incorrect. This payment reflects the correct code.',
  'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('66', 'Blood Deductible.', 'Start: 01/01/1995'),
 ('67', 'Lifetime reserve days. (Handled in QTY, QTY01=LA)', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('68', 'DRG weight. (Handled in CLP12)', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('69', 'Day outlier amount.', 'Start: 01/01/1995'),
 ('70',
  'Cost outlier - Adjustment to compensate for additional costs.',
  'Start: 01/01/1995 | Last Modified: 06/30/2001'),
 ('71', 'Primary Payer amount.', 'Start: 01/01/1995 | Stop: 06/30/2000\nNotes: Use code 23.'),
 ('72', 'Coinsurance day. (Handled in QTY, QTY01=CD)', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('73', 'Administrative days.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('74', 'Indirect Medical Education Adjustment.', 'Start: 01/01/1995'),
 ('75', 'Direct Medical Education Adjustment.', 'Start: 01/01/1995'),
 ('76', 'Disproportionate Share Adjustment.', 'Start: 01/01/1995'),
 ('77', 'Covered days. (Handled in QTY, QTY01=CA)', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('78', 'Non-Covered days/Room charge adjustment.', 'Start: 01/01/1995'),
 ('79', 'Cost Report days. (Handled in MIA15)', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('80', 'Outlier days. (Handled in QTY, QTY01=OU)', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('81', 'Discharges.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('82', 'PIP days.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('83', 'Total visits.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('84', 'Capital Adjustment. (Handled in MIA)', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('85',
  'Patient Interest Adjustment (Use Only Group code PR)',
  'Start: 01/01/1995 | Last Modified: 07/09/2007\n'
  'Notes: Only use when the payment of interest is the responsibility of the patient.'),
 ('86', 'Statutory Adjustment.', 'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Duplicative of code 45.'),
 ('87', 'Transfer amount.', 'Start: 01/01/1995 | Last Modified: 09/20/2009 | Stop: 01/01/2012'),
 ('88',
  'Adjustment amount represents collection against receivable created in prior overpayment.',
  'Start: 01/01/1995 | Stop: 06/30/2007'),
 ('89', 'Professional fees removed from charges.', 'Start: 01/01/1995'),
 ('90',
  'Ingredient cost adjustment. Usage: To be used for pharmaceuticals only.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('91', 'Dispensing fee adjustment.', 'Start: 01/01/1995'),
 ('92', 'Claim Paid in full.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('93',
  'No Claim level Adjustments.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: As of 004010, CAS at the claim level is optional.'),
 ('94', 'Processed in Excess of charges.', 'Start: 01/01/1995'),
 ('95', 'Plan procedures not followed.', 'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('96',
  'Non-covered charge(s). At least one Remark Code must be provided (may be comprised of either the NCPDP '
  'Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT.) Usage: Refer to the 835 '
  'Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('97',
  'The benefit for this service is included in the payment/allowance for another service/procedure that has '
  'already been adjudicated. Usage: Refer to the 835 Healthcare Policy Identification Segment (loop 2110 '
  'Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('98',
  'The hospital must file the Medicare claim for this inpatient non-physician service.',
  'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('99', 'Medicare Secondary Payer Adjustment Amount.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('100',
  'Payment made to patient/insured/responsible party.',
  'Start: 01/01/1995 | Last Modified: 05/01/2018'),
 ('101',
  'Predetermination: anticipated payment upon completion of services or claim adjudication.',
  'Start: 01/01/1995 | Last Modified: 02/28/1999'),
 ('102', 'Major Medical Adjustment.', 'Start: 01/01/1995'),
 ('103',
  'Provider promotional discount (e.g., Senior citizen discount).',
  'Start: 01/01/1995 | Last Modified: 06/30/2001'),
 ('104', 'Managed care withholding.', 'Start: 01/01/1995'),
 ('105', 'Tax withholding.', 'Start: 01/01/1995'),
 ('106', 'Patient payment option/election not in effect.', 'Start: 01/01/1995'),
 ('107',
  'The related or qualifying claim/service was not identified on this claim. Usage: Refer to the 835 '
  'Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('108',
  'Rent/purchase guidelines were not met. Usage: Refer to the 835 Healthcare Policy Identification Segment '
  '(loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('109',
  'Claim/service not covered by this payer/contractor. You must send the claim/service to the correct '
  'payer/contractor.',
  'Start: 01/01/1995 | Last Modified: 01/29/2012'),
 ('110', 'Billing date predates service date.', 'Start: 01/01/1995'),
 ('111', 'Not covered unless the provider accepts assignment.', 'Start: 01/01/1995'),
 ('112',
  'Service not furnished directly to the patient and/or not documented.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('113',
  'Payment denied because service/procedure was provided outside the United States or as a result of war.',
  'Start: 01/01/1995 | Last Modified: 02/28/2001 | Stop: 06/30/2007\nNotes: Use Codes 157, 158 or 159.'),
 ('114', 'Procedure/product not approved by the Food and Drug Administration.', 'Start: 01/01/1995'),
 ('115', 'Procedure postponed, canceled, or delayed.', 'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('116',
  'The advance indemnification notice signed by the patient did not comply with requirements.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('117',
  'Transportation is only covered to the closest facility that can provide the necessary care.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('118', 'ESRD network support adjustment.', 'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('119',
  'Benefit maximum for this time period or occurrence has been reached.',
  'Start: 01/01/1995 | Last Modified: 02/29/2004'),
 ('120',
  'Patient is covered by a managed care plan.',
  'Start: 01/01/1995 | Stop: 06/30/2007\nNotes: Use code 24.'),
 ('121',
  'Indemnification adjustment - compensation for outstanding member responsibility.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('122', 'Psychiatric reduction.', 'Start: 01/01/1995'),
 ('123',
  'Payer refund due to overpayment.',
  'Start: 01/01/1995 | Stop: 06/30/2007\n'
  'Notes: Refer to implementation guide for proper handling of reversals.'),
 ('124',
  'Payer refund amount - not our patient.',
  'Start: 01/01/1995 | Last Modified: 06/30/1999 | Stop: 06/30/2007\n'
  'Notes: Refer to implementation guide for proper handling of reversals.'),
 ('125',
  'Submission/billing error(s). At least one Remark Code must be provided (may be comprised of either the '
  'NCPDP Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT.)',
  'Start: 01/01/1995 | Last Modified: 09/20/2009 | Stop: 11/01/2013'),
 ('126',
  'Deductible -- Major Medical',
  'Start: 02/28/1997 | Last Modified: 09/30/2007 | Stop: 04/01/2008\nNotes: Use Group Code PR and code 1.'),
 ('127',
  'Coinsurance -- Major Medical',
  'Start: 02/28/1997 | Last Modified: 09/30/2007 | Stop: 04/01/2008\nNotes: Use Group Code PR and code 2.'),
 ('128', "Newborn's services are covered in the mother's Allowance.", 'Start: 02/28/1997'),
 ('129',
  'Prior processing information appears incorrect. At least one Remark Code must be provided (may be '
  'comprised of either the NCPDP Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT.)',
  'Start: 02/28/1997 | Last Modified: 01/30/2011'),
 ('130', 'Claim submission fee.', 'Start: 02/28/1997 | Last Modified: 06/30/2001'),
 ('131', 'Claim specific negotiated discount.', 'Start: 02/28/1997'),
 ('132', 'Prearranged demonstration project adjustment.', 'Start: 02/28/1997'),
 ('133',
  'The disposition of this service line is pending further review. (Use only with Group Code OA). Usage: Use '
  'of this code requires a reversal and correction when the service line is finalized (use only in Loop 2110 '
  'CAS segment of the 835 or Loop 2430 of the 837).',
  'Start: 07/01/2014 | Last Modified: 07/01/2017'),
 ('134', 'Technical fees removed from charges.', 'Start: 10/31/1998'),
 ('135', 'Interim bills cannot be processed.', 'Start: 10/31/1998 | Last Modified: 09/30/2007'),
 ('136',
  "Failure to follow prior payer's coverage rules. (Use only with Group Code OA)",
  'Start: 10/31/1998 | Last Modified: 07/01/2013'),
 ('137',
  'Regulatory Surcharges, Assessments, Allowances or Health Related Taxes.',
  'Start: 02/28/1999 | Last Modified: 09/30/2007'),
 ('138',
  'Appeal procedures not followed or time limits not met.',
  'Start: 06/30/1999 | Last Modified: 11/01/2017 | Stop: 05/01/2018'),
 ('139',
  'Contracted funding agreement - Subscriber is employed by the provider of services. Use only with Group '
  'Code CO.',
  'Start: 06/30/1999 | Last Modified: 05/01/2018'),
 ('140', 'Patient/Insured health identification number and name do not match.', 'Start: 06/30/1999'),
 ('141',
  'Claim spans eligible and ineligible periods of coverage.',
  'Start: 06/30/1999 | Last Modified: 09/30/2007 | Stop: 07/01/2012'),
 ('142', 'Monthly Medicaid patient liability amount.', 'Start: 06/30/2000 | Last Modified: 09/30/2007'),
 ('143', 'Portion of payment deferred.', 'Start: 02/28/2001'),
 ('144', 'Incentive adjustment, e.g. preferred product/service.', 'Start: 06/30/2001'),
 ('145',
  'Premium payment withholding',
  'Start: 06/30/2002 | Last Modified: 09/30/2007 | Stop: 04/01/2008\nNotes: Use Group Code CO and code 45.'),
 ('146',
  'Diagnosis was invalid for the date(s) of service reported.',
  'Start: 06/30/2002 | Last Modified: 09/30/2007'),
 ('147', 'Provider contracted/negotiated rate expired or not on file.', 'Start: 06/30/2002'),
 ('148',
  'Information from another provider was not provided or was insufficient/incomplete. At least one Remark '
  'Code must be provided (may be comprised of either the NCPDP Reject Reason Code, or Remittance Advice '
  'Remark Code that is not an ALERT.)',
  'Start: 06/30/2002 | Last Modified: 09/20/2009'),
 ('149', 'Lifetime benefit maximum has been reached for this service/benefit category.', 'Start: 10/31/2002'),
 ('150',
  'Payer deems the information submitted does not support this level of service.',
  'Start: 10/31/2002 | Last Modified: 09/30/2007'),
 ('151',
  'Payment adjusted because the payer deems the information submitted does not support this many/frequency '
  'of services.',
  'Start: 10/31/2002 | Last Modified: 01/27/2008'),
 ('152',
  'Payer deems the information submitted does not support this length of service. Usage: Refer to the 835 '
  'Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 10/31/2002 | Last Modified: 07/01/2017'),
 ('153',
  'Payer deems the information submitted does not support this dosage.',
  'Start: 10/31/2002 | Last Modified: 09/30/2007'),
 ('154',
  "Payer deems the information submitted does not support this day's supply.",
  'Start: 10/31/2002 | Last Modified: 09/30/2007'),
 ('155', 'Patient refused the service/procedure.', 'Start: 06/30/2003 | Last Modified: 09/30/2007'),
 ('156',
  'Flexible spending account payments. Note: Use code 187.',
  'Start: 09/30/2003 | Last Modified: 01/25/2009 | Stop: 10/01/2009'),
 ('157',
  'Service/procedure was provided as a result of an act of war.',
  'Start: 09/30/2003 | Last Modified: 09/30/2007'),
 ('158',
  'Service/procedure was provided outside of the United States.',
  'Start: 09/30/2003 | Last Modified: 09/30/2007'),
 ('159',
  'Service/procedure was provided as a result of terrorism.',
  'Start: 09/30/2003 | Last Modified: 09/30/2007'),
 ('160',
  'Injury/illness was the result of an activity that is a benefit exclusion.',
  'Start: 09/30/2003 | Last Modified: 09/30/2007'),
 ('161', 'Provider performance bonus', 'Start: 02/29/2004'),
 ('162',
  'State-mandated Requirement for Property and Casualty, see Claim Payment Remarks Code for specific '
  'explanation.',
  'Start: 02/29/2004 | Stop: 07/01/2014\nNotes: Use code P1'),
 ('163',
  'Attachment/other documentation referenced on the claim was not received.',
  'Start: 06/30/2004 | Last Modified: 06/02/2013'),
 ('164',
  'Attachment/other documentation referenced on the claim was not received in a timely fashion.',
  'Start: 06/30/2004 | Last Modified: 06/02/2013'),
 ('165', 'Referral absent or exceeded.', 'Start: 10/31/2004 | Last Modified: 11/01/2017 | Stop: 05/01/2018'),
 ('166',
  'These services were submitted after this payers responsibility for processing claims under this plan '
  'ended.',
  'Start: 02/28/2005'),
 ('167',
  'This (these) diagnosis(es) is (are) not covered. Usage: Refer to the 835 Healthcare Policy Identification '
  'Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 06/30/2005 | Last Modified: 07/01/2017'),
 ('168',
  "Service(s) have been considered under the patient's medical plan. Benefits are not available under this "
  'dental plan.',
  'Start: 06/30/2005 | Last Modified: 11/01/2017 | Stop: 05/01/2018'),
 ('169', 'Alternate benefit has been provided.', 'Start: 06/30/2005 | Last Modified: 09/30/2007'),
 ('170',
  'Payment is denied when performed/billed by this type of provider. Usage: Refer to the 835 Healthcare '
  'Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 06/30/2005 | Last Modified: 07/01/2017'),
 ('171',
  'Payment is denied when performed/billed by this type of provider in this type of facility. Usage: Refer '
  'to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if '
  'present.',
  'Start: 06/30/2005 | Last Modified: 07/01/2017'),
 ('172',
  'Payment is adjusted when performed/billed by a provider of this specialty. Usage: Refer to the 835 '
  'Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 06/30/2005 | Last Modified: 07/01/2017'),
 ('173',
  'Service/equipment was not prescribed by a physician.',
  'Start: 06/30/2005 | Last Modified: 07/01/2013'),
 ('174', 'Service was not prescribed prior to delivery.', 'Start: 06/30/2005 | Last Modified: 09/30/2007'),
 ('175', 'Prescription is incomplete.', 'Start: 06/30/2005 | Last Modified: 09/30/2007'),
 ('176', 'Prescription is not current.', 'Start: 06/30/2005 | Last Modified: 09/30/2007'),
 ('177',
  'Patient has not met the required eligibility requirements.',
  'Start: 06/30/2005 | Last Modified: 09/30/2007'),
 ('178',
  'Patient has not met the required spend down requirements.',
  'Start: 06/30/2005 | Last Modified: 09/30/2007'),
 ('179',
  'Patient has not met the required waiting requirements. Usage: Refer to the 835 Healthcare Policy '
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 06/30/2005 | Last Modified: 03/01/2017'),
 ('180',
  'Patient has not met the required residency requirements.',
  'Start: 06/30/2005 | Last Modified: 09/30/2007'),
 ('181',
  'Procedure code was invalid on the date of service.',
  'Start: 06/30/2005 | Last Modified: 09/30/2007'),
 ('182',
  'Procedure modifier was invalid on the date of service.',
  'Start: 06/30/2005 | Last Modified: 09/30/2007'),
 ('183',
  'The referring provider is not eligible to refer the service billed. Usage: Refer to the 835 Healthcare '
  'Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 06/30/2005 | Last Modified: 07/01/2017'),
 ('184',
  'The prescribing/ordering provider is not eligible to prescribe/order the service billed. Usage: Refer to '
  'the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 06/30/2005 | Last Modified: 07/01/2017'),
 ('185',
  'The rendering provider is not eligible to perform the service billed. Usage: Refer to the 835 Healthcare '
  'Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 06/30/2005 | Last Modified: 07/01/2017'),
 ('186', 'Level of care change adjustment.', 'Start: 06/30/2005 | Last Modified: 09/30/2007'),
 ('187',
  'Consumer Spending Account payments (includes but is not limited to Flexible Spending Account, Health '
  'Savings Account, Health Reimbursement Account, etc.)',
  'Start: 06/30/2005 | Last Modified: 01/25/2009'),
 ('188',
  'This product/procedure is only covered when used according to FDA recommendations.',
  'Start: 06/30/2005'),
 ('189',
  "'Not otherwise classified' or 'unlisted' procedure code (CPT/HCPCS) was billed when there is a specific "
  'procedure code for this procedure/service',
  'Start: 06/30/2005'),
 ('190',
  'Payment is included in the allowance for a Skilled Nursing Facility (SNF) qualified stay.',
  'Start: 10/31/2005'),
 ('191',
  "Not a work related injury/illness and thus not the liability of the workers' compensation carrier Note: "
  'If adjustment is at the Claim Level, the payer must send and the provider should refer to the 835 '
  "Insurance Policy Number Segment (Loop 2100 Other Claim Related Information REF qualifier 'IG') for the "
  'jurisdictional regulation. If adjustment is at the Line Level, the payer must send and the provider '
  'should refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment information '
  'REF)',
  'Start: 10/31/2005 | Last Modified: 10/17/2010 | Stop: 07/01/2014\nNotes: Use code P2'),
 ('192',
  'Non standard adjustment code from paper remittance. Usage: This code is to be used by providers/payers '
  'providing Coordination of Benefits information to another payer in the 837 transaction only. This code is '
  'only used when the non-standard code cannot be reasonably mapped to an existing Claims Adjustment Reason '
  'Code, specifically Deductible, Coinsurance and Co-payment.',
  'Start: 10/31/2005 | Last Modified: 07/01/2017'),
 ('193',
  'Original payment decision is being maintained. Upon review, it was determined that this claim was '
  'processed properly.',
  'Start: 02/28/2006 | Last Modified: 01/27/2008'),
 ('194',
  'Anesthesia performed by the operating physician, the assistant surgeon or the attending physician.',
  'Start: 02/28/2006 | Last Modified: 09/30/2007'),
 ('195',
  'Refund issued to an erroneous priority payer for this claim/service.',
  'Start: 02/28/2006 | Last Modified: 09/30/2007'),
 ('196',
  "Claim/service denied based on prior payer's coverage determination.",
  'Start: 06/30/2006 | Stop: 02/01/2007\nNotes: Use code 136.'),
 ('197',
  'Precertification/authorization/notification/pre-treatment absent.',
  'Start: 10/31/2006 | Last Modified: 05/01/2018'),
 ('198',
  'Precertification/notification/authorization/pre-treatment exceeded.',
  'Start: 10/31/2006 | Last Modified: 05/01/2018'),
 ('199', 'Revenue code and Procedure code do not match.', 'Start: 10/31/2006'),
 ('200', 'Expenses incurred during lapse in coverage', 'Start: 10/31/2006'),
 ('201',
  "Patient is responsible for amount of this claim/service through 'set aside arrangement' or other "
  'agreement. (Use only with Group Code PR) At least one Remark Code must be provided (may be comprised of '
  'either the NCPDP Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT.)',
  'Start: 10/31/2006 | Last Modified: 09/28/2014\n'
  "Notes: Not for use by Workers' Compensation payers; use code P3 instead."),
 ('202',
  'Non-covered personal comfort or convenience services.',
  'Start: 02/28/2007 | Last Modified: 09/30/2007'),
 ('203', 'Discontinued or reduced service.', 'Start: 02/28/2007 | Last Modified: 09/30/2007'),
 ('204',
  "This service/equipment/drug is not covered under the patient's current benefit plan",
  'Start: 02/28/2007'),
 ('205', 'Pharmacy discount card processing fee', 'Start: 07/09/2007'),
 ('206', 'National Provider Identifier - missing.', 'Start: 07/09/2007 | Last Modified: 09/30/2007'),
 ('207', 'National Provider identifier - Invalid format', 'Start: 07/09/2007 | Last Modified: 06/01/2008'),
 ('208', 'National Provider Identifier - Not matched.', 'Start: 07/09/2007 | Last Modified: 09/30/2007'),
 ('209',
  'Per regulatory or other agreement. The provider cannot collect this amount from the patient. However, '
  'this amount may be billed to subsequent payer. Refund to patient if collected. (Use only with Group code '
  'OA)',
  'Start: 07/09/2007 | Last Modified: 07/01/2013'),
 ('210',
  'Payment adjusted because pre-certification/authorization not received in a timely fashion',
  'Start: 07/09/2007'),
 ('211', 'National Drug Codes (NDC) not eligible for rebate, are not covered.', 'Start: 07/09/2007'),
 ('212', 'Administrative surcharges are not covered', 'Start: 11/05/2007'),
 ('213',
  'Non-compliance with the physician self referral prohibition legislation or payer policy.',
  'Start: 01/27/2008'),
 ('214',
  "Workers' Compensation claim adjudicated as non-compensable. This Payer not liable for claim or "
  'service/treatment. Note: If adjustment is at the Claim Level, the payer must send and the provider should '
  'refer to the 835 Insurance Policy Number Segment (Loop 2100 Other Claim Related Information REF qualifier '
  "'IG') for the jurisdictional regulation. If adjustment is at the Line Level, the payer must send and the "
  'provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment '
  "information REF). To be used for Workers' Compensation only",
  'Start: 01/27/2008 | Last Modified: 10/17/2010 | Stop: 07/01/2014\nNotes: Use code P4'),
 ('215', 'Based on subrogation of a third party settlement', 'Start: 01/27/2008'),
 ('216',
  "Based on the findings of a review organization or the payer's findings.",
  'Start: 01/27/2008 | Last Modified: 03/01/2025'),
 ('217',
  'Based on payer reasonable and customary fees. No maximum allowable defined by legislated fee arrangement. '
  '(Note: To be used for Property and Casualty only)',
  'Start: 01/27/2008 | Last Modified: 09/30/2012 | Stop: 07/01/2014\nNotes: Use code P5'),
 ('218',
  'Based on entitlement to benefits. Note: If adjustment is at the Claim Level, the payer must send and the '
  'provider should refer to the 835 Insurance Policy Number Segment (Loop 2100 Other Claim Related '
  "Information REF qualifier 'IG') for the jurisdictional regulation. If adjustment is at the Line Level, "
  'the payer must send and the provider should refer to the 835 Healthcare Policy Identification Segment '
  "(loop 2110 Service Payment information REF). To be used for Workers' Compensation only",
  'Start: 01/27/2008 | Last Modified: 10/17/2010 | Stop: 07/01/2014\nNotes: Use code P6'),
 ('219',
  'Based on extent of injury. Usage: If adjustment is at the Claim Level, the payer must send and the '
  'provider should refer to the 835 Insurance Policy Number Segment (Loop 2100 Other Claim Related '
  "Information REF qualifier 'IG') for the jurisdictional regulation. If adjustment is at the Line Level, "
  'the payer must send and the provider should refer to the 835 Healthcare Policy Identification Segment '
  '(loop 2110 Service Payment information REF).',
  'Start: 01/27/2008 | Last Modified: 07/01/2017'),
 ('220',
  'The applicable fee schedule/fee database does not contain the billed code. Please resubmit a bill with '
  'the appropriate fee schedule/fee database code(s) that best describe the service(s) provided and '
  'supporting documentation if required. (Note: To be used for Property and Casualty only)',
  'Start: 01/27/2008 | Last Modified: 09/30/2012 | Stop: 07/01/2014\nNotes: Use code P7'),
 ('221',
  'Claim is under investigation. Note: If adjustment is at the Claim Level, the payer must send and the '
  'provider should refer to the 835 Insurance Policy Number Segment (Loop 2100 Other Claim Related '
  "Information REF qualifier 'IG') for the jurisdictional regulation. If adjustment is at the Line Level, "
  'the payer must send and the provider should refer to the 835 Healthcare Policy Identification Segment '
  '(loop 2110 Service Payment information REF). (Note: To be used by Property & Casualty only)',
  'Start: 01/27/2008 | Last Modified: 07/01/2013 | Stop: 07/01/2014\nNotes: Use code P8'),
 ('222',
  'Exceeds the contracted maximum number of hours/days/units by this provider for this period. This is not '
  'patient specific. Usage: Refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service '
  'Payment Information REF), if present.',
  'Start: 06/01/2008 | Last Modified: 07/01/2017'),
 ('223',
  'Adjustment code for mandated federal, state or local law/regulation that is not already covered by '
  'another code and is mandated before a new code can be created.',
  'Start: 06/01/2008'),
 ('224',
  'Patient identification compromised by identity theft. Identity verification required for processing this '
  'and future claims.',
  'Start: 06/01/2008'),
 ('225',
  'Penalty or Interest Payment by Payer (Only used for plan to plan encounter reporting within the 837)',
  'Start: 06/01/2008'),
 ('226',
  'Information requested from the Billing/Rendering Provider was not provided or not provided timely or was '
  'insufficient/incomplete. At least one Remark Code must be provided (may be comprised of either the NCPDP '
  'Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT.)',
  'Start: 09/21/2008 | Last Modified: 07/01/2013'),
 ('227',
  'Information requested from the patient/insured/responsible party was not provided or was '
  'insufficient/incomplete. At least one Remark Code must be provided (may be comprised of either the NCPDP '
  'Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT.)',
  'Start: 09/21/2008 | Last Modified: 09/20/2009'),
 ('228',
  'Denied for failure of this provider, another provider or the subscriber to supply requested information '
  'to a previous payer for their adjudication',
  'Start: 09/21/2008'),
 ('229',
  'Partial charge amount not considered by Medicare due to the initial claim Type of Bill being 12X. Usage: '
  'This code can only be used in the 837 transaction to convey Coordination of Benefits information when the '
  "secondary payer's cost avoidance policy allows providers to bypass claim submission to a prior payer. "
  '(Use only with Group Code PR)',
  'Start: 01/25/2009 | Last Modified: 07/01/2017'),
 ('230',
  'No available or correlating CPT/HCPCS code to describe this service. Note: Used only by Property and '
  'Casualty.',
  'Start: 01/25/2009 | Stop: 07/01/2014\nNotes: Use code P9'),
 ('231',
  'Mutually exclusive procedures cannot be done in the same day/setting. Usage: Refer to the 835 Healthcare '
  'Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 07/01/2009 | Last Modified: 07/01/2017'),
 ('232',
  'Institutional Transfer Amount. Usage: Applies to institutional claims only and explains the DRG amount '
  'difference when the patient care crosses multiple institutions.',
  'Start: 11/01/2009 | Last Modified: 07/01/2017'),
 ('233',
  'Services/charges related to the treatment of a hospital-acquired condition or preventable medical error.',
  'Start: 01/24/2010'),
 ('234',
  'This procedure is not paid separately. At least one Remark Code must be provided (may be comprised of '
  'either the NCPDP Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT.)',
  'Start: 01/24/2010'),
 ('235', 'Sales Tax', 'Start: 06/06/2010'),
 ('236',
  'This procedure or procedure/modifier combination is not compatible with another procedure or '
  'procedure/modifier combination provided on the same day according to the National Correct Coding '
  'Initiative or workers compensation state regulations/ fee schedule requirements.',
  'Start: 01/30/2011 | Last Modified: 07/01/2013'),
 ('237',
  'Legislated/Regulatory Penalty. At least one Remark Code must be provided (may be comprised of either the '
  'NCPDP Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT.)',
  'Start: 06/05/2011'),
 ('238',
  'Claim spans eligible and ineligible periods of coverage, this is the reduction for the ineligible period. '
  '(Use only with Group Code PR)',
  'Start: 03/01/2012 | Last Modified: 07/01/2013'),
 ('239',
  'Claim spans eligible and ineligible periods of coverage. Rebill separate claims.',
  'Start: 03/01/2012 | Last Modified: 01/29/2012'),
 ('240',
  "The diagnosis is inconsistent with the patient's birth weight. Usage: Refer to the 835 Healthcare Policy "
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 06/03/2012 | Last Modified: 07/01/2017'),
 ('241', 'Low Income Subsidy (LIS) Co-payment Amount', 'Start: 06/03/2012'),
 ('242',
  'Services not provided by network/primary care providers.',
  'Start: 06/03/2012 | Last Modified: 06/02/2013\nNotes: This code replaces deactivated code 38'),
 ('243',
  'Services not authorized by network/primary care providers.',
  'Start: 06/03/2012 | Last Modified: 06/02/2013\nNotes: This code replaces deactivated code 38'),
 ('244',
  'Payment reduced to zero due to litigation. Additional information will be sent following the conclusion '
  'of litigation. To be used for Property & Casualty only.',
  'Start: 09/30/2012 | Stop: 07/01/2014\nNotes: Use code P10'),
 ('245', 'Provider performance program withhold.', 'Start: 09/30/2012'),
 ('246', 'This non-payable code is for required reporting only.', 'Start: 09/30/2012'),
 ('247',
  'Deductible for Professional service rendered in an Institutional setting and billed on an Institutional '
  'claim.',
  'Start: 09/30/2012\n'
  'Notes: For Medicare Bundled Payment use only, under the Patient Protection and Affordable Care Act '
  '(PPACA).'),
 ('248',
  'Coinsurance for Professional service rendered in an Institutional setting and billed on an Institutional '
  'claim.',
  'Start: 09/30/2012\n'
  'Notes: For Medicare Bundled Payment use only, under the Patient Protection and Affordable Care Act '
  '(PPACA).'),
 ('249',
  'This claim has been identified as a readmission. (Use only with Group Code CO)',
  'Start: 09/30/2012'),
 ('250',
  'The attachment/other documentation that was received was the incorrect attachment/document. The expected '
  'attachment/document is still missing. At least one Remark Code must be provided (may be comprised of '
  'either the NCPDP Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT).',
  'Start: 09/30/2012 | Last Modified: 06/01/2014'),
 ('251',
  'The attachment/other documentation that was received was incomplete or deficient. The necessary '
  'information is still needed to process the claim. At least one Remark Code must be provided (may be '
  'comprised of either the NCPDP Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT).',
  'Start: 09/30/2012 | Last Modified: 06/01/2014'),
 ('252',
  'An attachment/other documentation is required to adjudicate this claim/service. At least one Remark Code '
  'must be provided (may be comprised of either the NCPDP Reject Reason Code, or Remittance Advice Remark '
  'Code that is not an ALERT).',
  'Start: 09/30/2012 | Last Modified: 06/02/2013'),
 ('253', 'Sequestration - reduction in federal payment', 'Start: 06/02/2013 | Last Modified: 11/01/2013'),
 ('254',
  'Claim received by the dental plan, but benefits not available under this plan. Submit these services to '
  "the patient's medical plan for further consideration.",
  'Start: 06/02/2013 | Last Modified: 11/01/2017\nNotes: Use CARC 290 if the claim was forwarded.'),
 ('255',
  'The disposition of the related Property & Casualty claim (injury or illness) is pending due to '
  'litigation. (Use only with Group Code OA)',
  'Start: 06/02/2013 | Stop: 07/01/2014\nNotes: Use code P11'),
 ('256', 'Service not payable per managed care contract.', 'Start: 06/02/2013'),
 ('257',
  'The disposition of the claim/service is undetermined during the premium payment grace period, per Health '
  'Insurance Exchange requirements. This claim/service will be reversed and corrected when the grace period '
  'ends (due to premium payment or lack of premium payment). (Use only with Group Code OA)',
  'Start: 11/01/2013 | Last Modified: 06/01/2014\n'
  'Notes: To be used after the first month of the grace period.'),
 ('258',
  'Claim/service not covered when patient is in custody/incarcerated. Applicable federal, state or local '
  'authority may cover the claim/service.',
  'Start: 11/01/2013'),
 ('259', 'Additional payment for Dental/Vision service utilization.', 'Start: 01/26/2014'),
 ('260', 'Processed under Medicaid ACA Enhanced Fee Schedule', 'Start: 01/26/2014'),
 ('261', "The procedure or service is inconsistent with the patient's history.", 'Start: 06/01/2014'),
 ('262',
  'Adjustment for delivery cost. Usage: To be used for pharmaceuticals only.',
  'Start: 11/01/2014 | Last Modified: 07/01/2017'),
 ('263',
  'Adjustment for shipping cost. Usage: To be used for pharmaceuticals only.',
  'Start: 11/01/2014 | Last Modified: 07/01/2017'),
 ('264',
  'Adjustment for postage cost. Usage: To be used for pharmaceuticals only.',
  'Start: 11/01/2014 | Last Modified: 07/01/2017'),
 ('265',
  'Adjustment for administrative cost. Usage: To be used for pharmaceuticals only.',
  'Start: 11/01/2014 | Last Modified: 07/01/2017'),
 ('266',
  'Adjustment for compound preparation cost. Usage: To be used for pharmaceuticals only.',
  'Start: 11/01/2014 | Last Modified: 07/01/2017'),
 ('267',
  'Claim/service spans multiple months. At least one Remark Code must be provided (may be comprised of '
  'either the NCPDP Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT.)',
  'Start: 11/01/2014 | Last Modified: 04/01/2015'),
 ('268',
  'The Claim spans two calendar years. Please resubmit one claim per calendar year.',
  'Start: 11/01/2014'),
 ('269',
  'Anesthesia not covered for this service/procedure. Usage: Refer to the 835 Healthcare Policy '
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 03/01/2015 | Last Modified: 07/01/2017'),
 ('270',
  'Claim received by the medical plan, but benefits not available under this plan. Submit these services to '
  "the patient's dental plan for further consideration.",
  'Start: 07/01/2015 | Last Modified: 11/01/2017\nNotes: Use CARC 291 if the claim was forwarded.'),
 ('271',
  'Prior contractual reductions related to a current periodic payment as part of a contractual payment '
  'schedule when deferred amounts have been previously reported. (Use only with Group Code OA)',
  'Start: 11/01/2015 | Last Modified: 03/01/2018'),
 ('272', 'Coverage/program guidelines were not met.', 'Start: 11/01/2015'),
 ('273', 'Coverage/program guidelines were exceeded.', 'Start: 11/01/2015'),
 ('274', 'Fee/Service not payable per patient Care Coordination arrangement.', 'Start: 11/01/2015'),
 ('275',
  "Prior payer's (or payers') patient responsibility (deductible, coinsurance, co-payment) not covered. (Use "
  'only with Group Code PR)',
  'Start: 11/01/2015'),
 ('276', 'Services denied by the prior payer(s) are not covered by this payer.', 'Start: 11/01/2015'),
 ('277',
  'The disposition of the claim/service is undetermined during the premium payment grace period, per Health '
  'Insurance SHOP Exchange requirements. This claim/service will be reversed and corrected when the grace '
  'period ends (due to premium payment or lack of premium payment). (Use only with Group Code OA)',
  'Start: 11/01/2015\nNotes: To be used during 31 day SHOP grace period.'),
 ('278',
  'Performance program proficiency requirements not met. (Use only with Group Codes CO or PI) Usage: Refer '
  'to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment Information REF), if '
  'present.',
  'Start: 07/01/2016 | Last Modified: 07/01/2017'),
 ('279',
  'Services not provided by Preferred network providers. Usage: Use this code when there are member network '
  "limitations. For example, using contracted providers not in the member's 'narrow' network.",
  'Start: 11/01/2016 | Last Modified: 07/01/2017'),
 ('280',
  'Claim received by the medical plan, but benefits not available under this plan. Submit these services to '
  "the patient's Pharmacy plan for further consideration.",
  'Start: 03/01/2017 | Last Modified: 11/01/2017\nNotes: Use CARC 292 if the claim was forwarded.'),
 ('281', 'Deductible waived per contractual agreement. Use only with Group Code CO.', 'Start: 07/01/2017'),
 ('282',
  'The procedure/revenue code is inconsistent with the type of bill. Usage: Refer to the 835 Healthcare '
  'Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 07/01/2017'),
 ('283', 'Attending provider is not eligible to provide direction of care.', 'Start: 11/01/2017'),
 ('284',
  'Precertification/authorization/notification/pre-treatment number may be valid but does not apply to the '
  'billed services.',
  'Start: 11/01/2017'),
 ('285', 'Appeal procedures not followed', 'Start: 11/01/2017'),
 ('286', 'Appeal time limits not met', 'Start: 11/01/2017'),
 ('287', 'Referral exceeded', 'Start: 11/01/2017'),
 ('288', 'Referral absent', 'Start: 11/01/2017'),
 ('289',
  'Services considered under the dental and medical plans, benefits not available.',
  'Start: 11/01/2017\nNotes: Also see CARCs 254, 270 and 280.'),
 ('290',
  'Claim received by the dental plan, but benefits not available under this plan. Claim has been forwarded '
  "to the patient's medical plan for further consideration.",
  'Start: 11/01/2017\nNotes: Use CARC 254 if the claim was not forwarded.'),
 ('291',
  'Claim received by the medical plan, but benefits not available under this plan. Claim has been forwarded '
  "to the patient's dental plan for further consideration.",
  'Start: 11/01/2017\nNotes: Use CARC 270 if the claim was not forwarded.'),
 ('292',
  'Claim received by the medical plan, but benefits not available under this plan. Claim has been forwarded '
  "to the patient's pharmacy plan for further consideration.",
  'Start: 11/01/2017\nNotes: Use CARC 280 if the claim was not forwarded.'),
 ('293', 'Payment made to employer.', 'Start: 05/01/2018'),
 ('294', 'Payment made to attorney.', 'Start: 11/01/2017'),
 ('295', 'Pharmacy Direct/Indirect Remuneration (DIR)', 'Start: 03/01/2018'),
 ('296',
  'Precertification/authorization/notification/pre-treatment number may be valid but does not apply to the '
  'provider.',
  'Start: 07/01/2018'),
 ('297',
  'Claim received by the medical plan, but benefits not available under this plan. Submit these services to '
  "the patient's vision plan for further consideration.",
  'Start: 03/01/2019'),
 ('298',
  'Claim received by the medical plan, but benefits not available under this plan. Claim has been forwarded '
  "to the patient's vision plan for further consideration.",
  'Start: 03/01/2019'),
 ('299',
  'The billing provider is not eligible to receive payment for the service billed.',
  'Start: 07/01/2019'),
 ('300',
  'Claim received by the Medical Plan, but benefits not available under this plan. Claim has been forwarded '
  "to the patient's Behavioral Health Plan for further consideration.",
  'Start: 07/01/2019'),
 ('301',
  'Claim received by the Medical Plan, but benefits not available under this plan. Submit these services to '
  "the patient's Behavioral Health Plan for further consideration.",
  'Start: 07/01/2019'),
 ('302',
  'Precertification/notification/authorization/pre-treatment time limit has expired.',
  'Start: 11/01/2020'),
 ('303',
  "Prior payer's (or payers') patient responsibility (deductible, coinsurance, co-payment) not covered for "
  'Qualified Medicare and Medicaid Beneficiaries. (Use only with Group Code CO)',
  'Start: 07/01/2021'),
 ('304',
  'Claim received by the medical plan, but benefits not available under this plan. Submit these services to '
  "the patient's hearing plan for further consideration.",
  'Start: 03/01/2022'),
 ('305',
  'Claim received by the medical plan, but benefits not available under this plan. Claim has been forwarded '
  "to the patient's hearing plan for further consideration.",
  'Start: 03/01/2022'),
 ('306',
  'Type of bill is inconsistent with the patient status. Usage: Refer to the 835 Healthcare Policy '
  'Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 11/01/2023'),
 ('307',
  'Medicare Maximum Fair Price Standard Default Refund Amount Adjustment. At least one Remark Code must be '
  'provided (may be comprised of either the NCPDP Reject Reason Code, or Remittance Advice Remark Code that '
  'is not an ALERT.) Usage: To be used only for the Medicare Drug Price Negotiation Program.',
  'Start: 03/01/2025'),
 ('308',
  'Payment is adjusted due to contracted funding agreement between the payer and provider.',
  'Start: 11/01/2025'),
 ('A0', 'Patient refund amount.', 'Start: 01/01/1995'),
 ('A1',
  'Claim/Service denied. At least one Remark Code must be provided (may be comprised of either the NCPDP '
  'Reject Reason Code, or Remittance Advice Remark Code that is not an ALERT.) Usage: Use this code only '
  'when a more specific Claim Adjustment Reason Code is not available.',
  'Start: 01/01/1995 | Last Modified: 11/16/2022'),
 ('A2',
  'Contractual adjustment.',
  'Start: 01/01/1995 | Last Modified: 02/28/2007 | Stop: 01/01/2008\n'
  "Notes: Use Code 45 with Group Code 'CO' or use another appropriate specific adjustment code."),
 ('A3', 'Medicare Secondary Payer liability met.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('A4',
  'Medicare Claim PPS Capital Day Outlier Amount.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007 | Stop: 04/01/2008'),
 ('A5', 'Medicare Claim PPS Capital Cost Outlier Amount.', 'Start: 01/01/1995'),
 ('A6', 'Prior hospitalization or 30 day transfer requirement not met.', 'Start: 01/01/1995'),
 ('A7', 'Presumptive Payment Adjustment', 'Start: 01/01/1995 | Stop: 07/01/2015'),
 ('A8', 'Ungroupable DRG.', 'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('B1', 'Non-covered visits.', 'Start: 01/01/1995'),
 ('B2', 'Covered visits.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('B3', 'Covered charges.', 'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('B4', 'Late filing penalty.', 'Start: 01/01/1995'),
 ('B5',
  'Coverage/program guidelines were not met or were exceeded.',
  'Start: 01/01/1995 | Last Modified: 11/01/2015 | Stop: 05/01/2016\n'
  'Notes: This code has been replaced by 272 and 273.'),
 ('B6',
  'This payment is adjusted when performed/billed by this type of provider, by this type of provider in this '
  'type of facility, or by a provider of this specialty.',
  'Start: 01/01/1995 | Stop: 02/01/2006'),
 ('B7',
  'This provider was not certified/eligible to be paid for this procedure/service on this date of service. '
  'Usage: Refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment Information '
  'REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('B8',
  'Alternative services were available, and should have been utilized. Usage: Refer to the 835 Healthcare '
  'Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('B9', 'Patient is enrolled in a Hospice.', 'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('B10',
  'Allowed amount has been reduced because a component of the basic procedure/test was paid. The beneficiary '
  'is not liable for more than the charge limit for the basic procedure/test.',
  'Start: 01/01/1995'),
 ('B11',
  'The claim/service has been transferred to the proper payer/processor for processing. Claim/service not '
  'covered by this payer/processor.',
  'Start: 01/01/1995'),
 ('B12',
  "Services not documented in patient's medical records.",
  'Start: 01/01/1995 | Last Modified: 03/01/2018'),
 ('B13',
  'Previously paid. Payment for this claim/service may have been provided in a previous payment.',
  'Start: 01/01/1995'),
 ('B14',
  'Only one visit or consultation per physician per day is covered.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('B15',
  'This service/procedure requires that a qualifying service/procedure be received and covered. The '
  'qualifying other service/procedure has not been received/adjudicated. Usage: Refer to the 835 Healthcare '
  'Policy Identification Segment (loop 2110 Service Payment Information REF), if present.',
  'Start: 01/01/1995 | Last Modified: 07/01/2017'),
 ('B16', "'New Patient' qualifications were not met.", 'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('B17',
  'Payment adjusted because this service was not prescribed by a physician, not prescribed prior to '
  'delivery, the prescription is incomplete, or the prescription is not current.',
  'Start: 01/01/1995 | Stop: 02/01/2006'),
 ('B18',
  'This procedure code and modifier were invalid on the date of service.',
  'Start: 01/01/1995 | Last Modified: 09/21/2008 | Stop: 03/01/2009'),
 ('B19',
  'Claim/service adjusted because of the finding of a Review Organization.',
  'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('B20',
  'Procedure/service was partially or fully furnished by another provider.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('B21',
  'The charges were reduced because the service/care was partially furnished by another physician.',
  'Start: 01/01/1995 | Stop: 10/16/2003'),
 ('B22', 'This payment is adjusted based on the diagnosis.', 'Start: 01/01/1995 | Last Modified: 02/28/2001'),
 ('B23',
  'Procedure billed is not authorized per your Clinical Laboratory Improvement Amendment (CLIA) proficiency '
  'test.',
  'Start: 01/01/1995 | Last Modified: 09/30/2007'),
 ('D1',
  'Claim/service denied. Level of subluxation is missing or inadequate.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 16 and remark codes if necessary.'),
 ('D2',
  'Claim lacks the name, strength, or dosage of the drug furnished.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 16 and remark codes if necessary.'),
 ('D3',
  'Claim/service denied because information to indicate if the patient owns the equipment that requires the '
  'part or supply was missing.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 16 and remark codes if necessary.'),
 ('D4',
  'Claim/service does not indicate the period of time for which this will be needed.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 16 and remark codes if necessary.'),
 ('D5',
  'Claim/service denied. Claim lacks individual lab codes included in the test.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 16 and remark codes if necessary.'),
 ('D6',
  "Claim/service denied. Claim did not include patient's medical record for the service.",
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 16 and remark codes if necessary.'),
 ('D7',
  "Claim/service denied. Claim lacks date of patient's most recent physician visit.",
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 16 and remark codes if necessary.'),
 ('D8',
  "Claim/service denied. Claim lacks indicator that 'x-ray is available for review.'",
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 16 and remark codes if necessary.'),
 ('D9',
  'Claim/service denied. Claim lacks invoice or statement certifying the actual cost of the lens, less '
  'discounts or the type of intraocular lens used.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 16 and remark codes if necessary.'),
 ('D10',
  'Claim/service denied. Completed physician financial relationship form not on file.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 17.'),
 ('D11',
  'Claim lacks completed pacemaker registration form.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 17.'),
 ('D12',
  'Claim/service denied. Claim does not identify who performed the purchased diagnostic test or the amount '
  'you were charged for the test.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 17.'),
 ('D13',
  'Claim/service denied. Performed by a facility/supplier in which the ordering/referring physician has a '
  'financial interest.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 17.'),
 ('D14',
  'Claim lacks indication that plan of treatment is on file.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 17.'),
 ('D15',
  'Claim lacks indication that service was supervised or evaluated by a physician.',
  'Start: 01/01/1995 | Stop: 10/16/2003\nNotes: Use code 17.'),
 ('D16',
  'Claim lacks prior payer payment information.',
  'Start: 01/01/1995 | Stop: 06/30/2007\n'
  'Notes: Use code 16 with appropriate claim payment remark code [N4].'),
 ('D17',
  'Claim/Service has invalid non-covered days.',
  'Start: 01/01/1995 | Stop: 06/30/2007\nNotes: Use code 16 with appropriate claim payment remark code.'),
 ('D18',
  'Claim/Service has missing diagnosis information.',
  'Start: 01/01/1995 | Stop: 06/30/2007\nNotes: Use code 16 with appropriate claim payment remark code.'),
 ('D19',
  'Claim/Service lacks Physician/Operative or other supporting documentation',
  'Start: 01/01/1995 | Stop: 06/30/2007\nNotes: Use code 16 with appropriate claim payment remark code.'),
 ('D20',
  'Claim/Service missing service/product information.',
  'Start: 01/01/1995 | Stop: 06/30/2007\nNotes: Use code 16 with appropriate claim payment remark code.'),
 ('D21',
  'This (these) diagnosis(es) is (are) missing or are invalid',
  'Start: 01/01/1995 | Stop: 06/30/2007'),
 ('D22',
  'Reimbursement was adjusted for the reasons to be provided in separate correspondence. (Note: To be used '
  "for Workers' Compensation only) - Temporary code to be added for timeframe only until 01/01/2009. Another "
  'code to be established and/or for 06/2008 meeting for a revised code to replace or strategy to use '
  'another existing code',
  'Start: 01/27/2008 | Stop: 01/01/2009'),
 ('D23',
  'This dual eligible patient is covered by Medicare Part D per Medicare Retro-Eligibility. At least one '
  'Remark Code must be provided (may be comprised of either the NCPDP Reject Reason Code, or Remittance '
  'Advice Remark Code that is not an ALERT.)',
  'Start: 11/01/2009 | Stop: 01/01/2012'),
 ('P1',
  'State-mandated Requirement for Property and Casualty, see Claim Payment Remarks Code for specific '
  'explanation. To be used for Property and Casualty only.',
  'Start: 11/01/2013\nNotes: This code replaces deactivated code 162'),
 ('P2',
  "Not a work related injury/illness and thus not the liability of the workers' compensation carrier Usage: "
  'If adjustment is at the Claim Level, the payer must send and the provider should refer to the 835 '
  "Insurance Policy Number Segment (Loop 2100 Other Claim Related Information REF qualifier 'IG') for the "
  'jurisdictional regulation. If adjustment is at the Line Level, the payer must send and the provider '
  'should refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment information '
  "REF). To be used for Workers' Compensation only.",
  'Start: 11/01/2013 | Last Modified: 07/01/2017\nNotes: This code replaces deactivated code 191'),
 ('P3',
  "Workers' Compensation case settled. Patient is responsible for amount of this claim/service through WC "
  "'Medicare set aside arrangement' or other agreement. To be used for Workers' Compensation only. (Use only "
  'with Group Code PR)',
  'Start: 11/01/2013\nNotes: This code replaces deactivated code 201'),
 ('P4',
  "Workers' Compensation claim adjudicated as non-compensable. This Payer not liable for claim or "
  'service/treatment. Usage: If adjustment is at the Claim Level, the payer must send and the provider '
  'should refer to the 835 Insurance Policy Number Segment (Loop 2100 Other Claim Related Information REF '
  "qualifier 'IG') for the jurisdictional regulation. If adjustment is at the Line Level, the payer must "
  'send and the provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service '
  "Payment information REF). To be used for Workers' Compensation only",
  'Start: 11/01/2013 | Last Modified: 07/01/2017\nNotes: This code replaces deactivated code 214'),
 ('P5',
  'Based on payer reasonable and customary fees. No maximum allowable defined by legislated fee arrangement. '
  'To be used for Property and Casualty only.',
  'Start: 11/01/2013\nNotes: This code replaces deactivated code 217'),
 ('P6',
  'Based on entitlement to benefits. Usage: If adjustment is at the Claim Level, the payer must send and the '
  'provider should refer to the 835 Insurance Policy Number Segment (Loop 2100 Other Claim Related '
  "Information REF qualifier 'IG') for the jurisdictional regulation. If adjustment is at the Line Level, "
  'the payer must send and the provider should refer to the 835 Healthcare Policy Identification Segment '
  '(loop 2110 Service Payment information REF). To be used for Property and Casualty only.',
  'Start: 11/01/2013 | Last Modified: 07/01/2017\nNotes: This code replaces deactivated code 218'),
 ('P7',
  'The applicable fee schedule/fee database does not contain the billed code. Please resubmit a bill with '
  'the appropriate fee schedule/fee database code(s) that best describe the service(s) provided and '
  'supporting documentation if required. To be used for Property and Casualty only.',
  'Start: 11/01/2013\nNotes: This code replaces deactivated code 220'),
 ('P8',
  'Claim is under investigation. Usage: If adjustment is at the Claim Level, the payer must send and the '
  'provider should refer to the 835 Insurance Policy Number Segment (Loop 2100 Other Claim Related '
  "Information REF qualifier 'IG') for the jurisdictional regulation. If adjustment is at the Line Level, "
  'the payer must send and the provider should refer to the 835 Healthcare Policy Identification Segment '
  '(loop 2110 Service Payment information REF). To be used for Property and Casualty only.',
  'Start: 11/01/2013 | Last Modified: 07/01/2017\nNotes: This code replaces deactivated code 221'),
 ('P9',
  'No available or correlating CPT/HCPCS code to describe this service. To be used for Property and Casualty '
  'only.',
  'Start: 11/01/2013\nNotes: This code replaces deactivated code 230'),
 ('P10',
  'Payment reduced to zero due to litigation. Additional information will be sent following the conclusion '
  'of litigation. To be used for Property and Casualty only.',
  'Start: 11/01/2013\nNotes: This code replaces deactivated code 244'),
 ('P11',
  'The disposition of the related Property & Casualty claim (injury or illness) is pending due to '
  'litigation. To be used for Property and Casualty only. (Use only with Group Code OA)',
  'Start: 11/01/2013\nNotes: This code replaces deactivated code 255'),
 ('P12',
  "Workers' compensation jurisdictional fee schedule adjustment. Usage: If adjustment is at the Claim Level, "
  'the payer must send and the provider should refer to the 835 Class of Contract Code Identification '
  'Segment (Loop 2100 Other Claim Related Information REF). If adjustment is at the Line Level, the payer '
  'must send and the provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 '
  "Service Payment information REF) if the regulations apply. To be used for Workers' Compensation only.",
  'Start: 11/01/2013 | Last Modified: 07/01/2017\nNotes: This code replaces deactivated code W1'),
 ('P13',
  "Payment reduced or denied based on workers' compensation jurisdictional regulations or payment policies, "
  'use only if no other code is applicable. Usage: If adjustment is at the Claim Level, the payer must send '
  'and the provider should refer to the 835 Insurance Policy Number Segment (Loop 2100 Other Claim Related '
  "Information REF qualifier 'IG') if the jurisdictional regulation applies. If adjustment is at the Line "
  'Level, the payer must send and the provider should refer to the 835 Healthcare Policy Identification '
  "Segment (loop 2110 Service Payment information REF) if the regulations apply. To be used for Workers' "
  'Compensation only.',
  'Start: 11/01/2013 | Last Modified: 07/01/2017\nNotes: This code replaces deactivated code W2'),
 ('P14',
  'The Benefit for this Service is included in the payment/allowance for another service/procedure that has '
  'been performed on the same day. Usage: Refer to the 835 Healthcare Policy Identification Segment (loop '
  '2110 Service Payment Information REF), if present. To be used for Property and Casualty only.',
  'Start: 11/01/2013 | Last Modified: 07/01/2017\nNotes: This code replaces deactivated code W3'),
 ('P15',
  "Workers' Compensation Medical Treatment Guideline Adjustment. To be used for Workers' Compensation only.",
  'Start: 11/01/2013\nNotes: This code replaces deactivated code W4'),
 ('P16',
  'Medical provider not authorized/certified to provide treatment to injured workers in this jurisdiction. '
  "To be used for Workers' Compensation only. (Use with Group Code CO or OA)",
  'Start: 11/01/2013\nNotes: This code replaces deactivated code W5'),
 ('P17',
  'Referral not authorized by attending physician per regulatory requirement. To be used for Property and '
  'Casualty only.',
  'Start: 11/01/2013\nNotes: This code replaces deactivated code W6'),
 ('P18',
  'Procedure is not listed in the jurisdiction fee schedule. An allowance has been made for a comparable '
  'service. To be used for Property and Casualty only.',
  'Start: 11/01/2013\nNotes: This code replaces deactivated code W7'),
 ('P19',
  'Procedure has a relative value of zero in the jurisdiction fee schedule, therefore no payment is due. To '
  'be used for Property and Casualty only.',
  'Start: 11/01/2013\nNotes: This code replaces deactivated code W8'),
 ('P20',
  'Service not paid under jurisdiction allowed outpatient facility fee schedule. To be used for Property and '
  'Casualty only.',
  'Start: 11/01/2013\nNotes: This code replaces deactivated code W9'),
 ('P21',
  'Payment denied based on the Medical Payments Coverage (MPC) and/or Personal Injury Protection (PIP) '
  'Benefits jurisdictional regulations, or payment policies. Usage: If adjustment is at the Claim Level, the '
  'payer must send and the provider should refer to the 835 Insurance Policy Number Segment (Loop 2100 Other '
  "Claim Related Information REF qualifier 'IG') if the jurisdictional regulation applies. If adjustment is "
  'at the Line Level, the payer must send and the provider should refer to the 835 Healthcare Policy '
  'Identification Segment (loop 2110 Service Payment information REF) if the regulations apply. To be used '
  'for Property and Casualty Auto only.',
  'Start: 11/01/2013 | Last Modified: 03/01/2018\nNotes: This code replaces deactivated code Y1'),
 ('P22',
  'Payment adjusted based on the Medical Payments Coverage (MPC) and/or Personal Injury Protection (PIP) '
  'Benefits jurisdictional regulations, or payment policies. Usage: If adjustment is at the Claim Level, the '
  'payer must send and the provider should refer to the 835 Insurance Policy Number Segment (Loop 2100 Other '
  "Claim Related Information REF qualifier 'IG') if the jurisdictional regulation applies. If adjustment is "
  'at the Line Level, the payer must send and the provider should refer to the 835 Healthcare Policy '
  'Identification Segment (loop 2110 Service Payment information REF) if the regulations apply. To be used '
  'for Property and Casualty Auto only.',
  'Start: 11/01/2013 | Last Modified: 03/01/2018\nNotes: This code replaces deactivated code Y2'),
 ('P23',
  'Medical Payments Coverage (MPC) or Personal Injury Protection (PIP) Benefits jurisdictional fee schedule '
  'adjustment. Usage: If adjustment is at the Claim Level, the payer must send and the provider should refer '
  'to the 835 Class of Contract Code Identification Segment (Loop 2100 Other Claim Related Information REF). '
  'If adjustment is at the Line Level, the payer must send and the provider should refer to the 835 '
  'Healthcare Policy Identification Segment (loop 2110 Service Payment information REF) if the regulations '
  'apply. To be used for Property and Casualty Auto only.',
  'Start: 11/01/2013 | Last Modified: 07/01/2017\nNotes: This code replaces deactivated code Y3'),
 ('P24',
  'Payment adjusted based on Preferred Provider Organization (PPO). Usage: If adjustment is at the Claim '
  'Level, the payer must send and the provider should refer to the 835 Class of Contract Code Identification '
  'Segment (Loop 2100 Other Claim Related Information REF). If adjustment is at the Line Level, the payer '
  'must send and the provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 '
  'Service Payment information REF) if the regulations apply. To be used for Property and Casualty only. Use '
  'only with Group Code CO.',
  'Start: 11/01/2017'),
 ('P25',
  'Payment adjusted based on Medical Provider Network (MPN). Usage: If adjustment is at the Claim Level, the '
  'payer must send and the provider should refer to the 835 Class of Contract Code Identification Segment '
  '(Loop 2100 Other Claim Related Information REF). If adjustment is at the Line Level, the payer must send '
  'and the provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service '
  'Payment information REF) if the regulations apply. To be used for Property and Casualty only. (Use only '
  'with Group Code CO).',
  'Start: 11/01/2017'),
 ('P26',
  'Payment adjusted based on Voluntary Provider network (VPN). Usage: If adjustment is at the Claim Level, '
  'the payer must send and the provider should refer to the 835 Class of Contract Code Identification '
  'Segment (Loop 2100 Other Claim Related Information REF). If adjustment is at the Line Level, the payer '
  'must send and the provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 '
  'Service Payment information REF) if the regulations apply. To be used for Property and Casualty only. '
  '(Use only with Group Code CO).',
  'Start: 11/01/2017'),
 ('P27',
  'Payment denied based on the Liability Coverage Benefits jurisdictional regulations and/or payment '
  'policies. Usage: If adjustment is at the Claim Level, the payer must send and the provider should refer '
  "to the 835 Insurance Policy Number Segment (Loop 2100 Other Claim Related Information REF qualifier 'IG') "
  'if the jurisdictional regulation applies. If adjustment is at the Line Level, the payer must send and the '
  'provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment '
  'information REF) if the regulations apply. To be used for Property and Casualty Auto only.',
  'Start: 11/01/2017'),
 ('P28',
  'Payment adjusted based on the Liability Coverage Benefits jurisdictional regulations and/or payment '
  'policies. Usage: If adjustment is at the Claim Level, the payer must send and the provider should refer '
  "to the 835 Insurance Policy Number Segment (Loop 2100 Other Claim Related Information REF qualifier 'IG') "
  'if the jurisdictional regulation applies. If adjustment is at the Line Level, the payer must send and the '
  'provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment '
  'information REF) if the regulations apply. To be used for Property and Casualty Auto only.',
  'Start: 11/01/2017'),
 ('P29',
  'Liability Benefits jurisdictional fee schedule adjustment. Usage: If adjustment is at the Claim Level, '
  'the payer must send and the provider should refer to the 835 Class of Contract Code Identification '
  'Segment (Loop 2100 Other Claim Related Information REF). If adjustment is at the Line Level, the payer '
  'must send and the provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 '
  'Service Payment information REF) if the regulations apply. To be used for Property and Casualty Auto '
  'only.',
  'Start: 11/01/2017'),
 ('P30',
  'Payment denied for exacerbation when supporting documentation was not complete. To be used for Property '
  'and Casualty only.',
  'Start: 11/01/2020'),
 ('P31',
  'Payment denied for exacerbation when treatment exceeds time allowed. To be used for Property and Casualty '
  'only.',
  'Start: 11/01/2020'),
 ('P32', 'Payment adjusted due to Apportionment.', 'Start: 08/01/2022'),
 ('W1',
  "Workers' compensation jurisdictional fee schedule adjustment. Note: If adjustment is at the Claim Level, "
  'the payer must send and the provider should refer to the 835 Class of Contract Code Identification '
  'Segment (Loop 2100 Other Claim Related Information REF). If adjustment is at the Line Level, the payer '
  'must send and the provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 '
  'Service Payment information REF) if the regulations apply.',
  'Start: 02/29/2000 | Last Modified: 06/02/2013 | Stop: 07/01/2014\nNotes: Use code P12'),
 ('W2',
  "Payment reduced or denied based on workers' compensation jurisdictional regulations or payment policies, "
  'use only if no other code is applicable. Note: If adjustment is at the Claim Level, the payer must send '
  'and the provider should refer to the 835 Insurance Policy Number Segment (Loop 2100 Other Claim Related '
  "Information REF qualifier 'IG') if the jurisdictional regulation applies. If adjustment is at the Line "
  'Level, the payer must send and the provider should refer to the 835 Healthcare Policy Identification '
  "Segment (loop 2110 Service Payment information REF) if the regulations apply. To be used for Workers' "
  'Compensation only.',
  'Start: 10/17/2010 | Last Modified: 06/02/2013 | Stop: 07/01/2014\nNotes: Use code P13'),
 ('W3',
  'The Benefit for this Service is included in the payment/allowance for another service/procedure that has '
  'been performed on the same day. Note: Refer to the 835 Healthcare Policy Identification Segment (loop '
  '2110 Service Payment Information REF), if present. For use by Property and Casualty only.',
  'Start: 09/30/2012 | Stop: 07/01/2014\nNotes: Use code P14'),
 ('W4',
  "Workers' Compensation Medical Treatment Guideline Adjustment.",
  'Start: 09/30/2012 | Stop: 07/01/2014\nNotes: Use code P15'),
 ('W5',
  'Medical provider not authorized/certified to provide treatment to injured workers in this jurisdiction. '
  '(Use with Group Code CO or OA)',
  'Start: 06/02/2013 | Stop: 07/01/2014\nNotes: Use code P16'),
 ('W6',
  'Referral not authorized by attending physician per regulatory requirement.',
  'Start: 06/02/2013 | Stop: 07/01/2014\nNotes: Use code P17'),
 ('W7',
  'Procedure is not listed in the jurisdiction fee schedule. An allowance has been made for a comparable '
  'service.',
  'Start: 06/02/2013 | Stop: 07/01/2014\nNotes: Use code P18'),
 ('W8',
  'Procedure has a relative value of zero in the jurisdiction fee schedule, therefore no payment is due.',
  'Start: 06/02/2013 | Stop: 07/01/2014\nNotes: Use code P19'),
 ('W9',
  'Service not paid under jurisdiction allowed outpatient facility fee schedule.',
  'Start: 06/02/2013 | Stop: 07/01/2014\nNotes: Use code P20'),
 ('Y1',
  'Payment denied based on Medical Payments Coverage (MPC) or Personal Injury Protection (PIP) Benefits '
  'jurisdictional regulations or payment policies, use only if no other code is applicable. Note: If '
  'adjustment is at the Claim Level, the payer must send and the provider should refer to the 835 Insurance '
  "Policy Number Segment (Loop 2100 Other Claim Related Information REF qualifier 'IG') if the "
  'jurisdictional regulation applies. If adjustment is at the Line Level, the payer must send and the '
  'provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment '
  'information REF) if the regulations apply. To be used for P&C Auto only.',
  'Start: 09/30/2012 | Last Modified: 06/02/2013 | Stop: 07/01/2014\nNotes: Use code P21'),
 ('Y2',
  'Payment adjusted based on Medical Payments Coverage (MPC) or Personal Injury Protection (PIP) Benefits '
  'jurisdictional regulations or payment policies, use only if no other code is applicable. Note: If '
  'adjustment is at the Claim Level, the payer must send and the provider should refer to the 835 Insurance '
  "Policy Number Segment (Loop 2100 Other Claim Related Information REF qualifier 'IG') if the "
  'jurisdictional regulation applies. If adjustment is at the Line Level, the payer must send and the '
  'provider should refer to the 835 Healthcare Policy Identification Segment (loop 2110 Service Payment '
  'information REF) if the regulations apply. To be used for P&C Auto only.',
  'Start: 09/30/2012 | Last Modified: 06/02/2013 | Stop: 07/01/2014\nNotes: Use code P22'),
 ('Y3',
  'Medical Payments Coverage (MPC) or Personal Injury Protection (PIP) Benefits jurisdictional fee schedule '
  'adjustment. Note: If adjustment is at the Claim Level, the payer must send and the provider should refer '
  'to the 835 Class of Contract Code Identification Segment (Loop 2100 Other Claim Related Information REF). '
  'If adjustment is at the Line Level, the payer must send and the provider should refer to the 835 '
  'Healthcare Policy Identification Segment (loop 2110 Service Payment information REF) if the regulations '
  'apply. To be used for P&C Auto only.',
  'Start: 09/30/2012 | Last Modified: 06/02/2013 | Stop: 07/01/2014\nNotes: Use code P23')]


def seed_reasons(apps, schema_editor):
    Reason = apps.get_model("edi835", "ClaimAdjustmentReason")
    for code, description, metadata in REASONS:
        Reason.objects.using(schema_editor.connection.alias).update_or_create(
            code=code, defaults={"description": description, "source_metadata": metadata})


class Migration(migrations.Migration):
    dependencies = [("edi835", "0069_intake_control_constraints")]
    operations = [
        migrations.CreateModel(
            name="ClaimAdjustmentReason",
            fields=[
                ("code", models.CharField(max_length=10, primary_key=True, serialize=False)),
                ("description", models.TextField()),
                ("source_metadata", models.TextField(blank=True, default="")),
            ],
            options={"db_table": "claim_adjustment_reason"},
        ),
        migrations.RunPython(seed_reasons, migrations.RunPython.noop),
    ]
