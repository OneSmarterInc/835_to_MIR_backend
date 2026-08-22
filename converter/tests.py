import os
from django.test import TestCase, Client
from converter.services.parser import parse_835_to_mir
from converter.services.validator import PyX12Validator

SAMPLE_ONE_LINE = "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *260813*1200*U*00501*000000001*0*P*:~GS*HP*SENDER*RECEIVER*20260813*1200*1*X*005010X221A1~ST*835*0001~BPR*I*150.00*C*CHK************20260813~TRN*1*123456789*1999999999~N1*PR*PAYER NAME~N1*PE*PROVIDER NAME*XX*1234567890~LX*1~CLP*CLAIM1001*1*200.00*150.00*50.00*MC*REF12345~NM1*QC*1*SMITH*JOHN*M~NM1*IL*1*SMITH*JOHN****MI*SUB123456~REF*1L*GRP999~DTM*036*19850101~DTM*050*20260801~SVC*HC:99213*200.00*150.00**1~DTM*472*20260805~CAS*CO*45*50.00~SE*16*0001~GE*1*1~IEA*1*000000001~"

SAMPLE_CRLF = SAMPLE_ONE_LINE.replace("~", "~\r\n")

class PyX12ValidatorTestSuite(TestCase):

    def setUp(self):
        self.validator = PyX12Validator()

    def test_1_valid_835(self):
        res = self.validator.validate(SAMPLE_ONE_LINE)
        self.assertEqual(res['total_segments'], 20)
        self.assertEqual(res['claims'], 1)
        self.assertEqual(res['validator_engine'], "Validated using PyX12")

    def test_2_malformed_isa(self):
        malformed_isa = "ISA*00*BAD_ISA_HEADER~ST*835*0001~SE*2*0001~"
        res = self.validator.validate(malformed_isa)
        self.assertFalse(res['valid'])
        self.assertGreater(len(res['errors']), 0)

    def test_7_non_835_x12(self):
        edi_270 = SAMPLE_ONE_LINE.replace("ST*835*0001~", "ST*270*0001~")
        res = self.validator.validate(edi_270)
        self.assertFalse(res['valid'])
        self.assertEqual(res['errors'][0]['code'], 'NON_835_TRANSACTION')


class ViewsTestCase(TestCase):

    def setUp(self):
        from accounts.models import User
        self.client = Client()
        self.user = User.objects.create_user(
            email="testuser@example.com",
            name="Test User",
            mobile="+15550000",
            password="testpassword"
        )
        # Log user in
        self.client.login(email="testuser@example.com", password="testpassword")

    def test_api_convert(self):
        response = self.client.post('/api/convert/', data={'edi_text': SAMPLE_ONE_LINE})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])

    def test_api_validate_endpoint(self):
        response = self.client.post('/api/validate/', data={'edi_text': SAMPLE_ONE_LINE})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['report']['total_segments'], 20)

    def test_health_check_endpoint(self):
        response = self.client.get('/health/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'healthy')
        self.assertEqual(data['database'], 'connected')
