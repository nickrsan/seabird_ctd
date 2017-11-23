import unittest
import logging
import os

import seabird_ctd

from seabird_ctd import local_test_settings  # machine-specific file with attributes COM_POT and BAUD_RATE

log = logging.getLogger("seabird_ctd")
logging.basicConfig(level=logging.DEBUG)

class CTDTest(unittest.TestCase):
	def setUp(self):
		self.ctd = seabird_ctd.CTD(local_test_settings.COM_PORT, baud=local_test_settings.BAUD_RATE)

	def test_data_read(self):

		try:
			self.assertTrue(hasattr(self.ctd, "sample_number"))  # this gets assigned after initialization, and comes in when status info is read
		finally:
			self.ctd.sleep()
			self.ctd.ctd.close()

	def test_ctd_deletion(self):
		"""
			We had an issue where upon exiting python or deleting the CTD object, it raised an Attribute Error because
			it attempted to read from the CTD after sending the QS command. Resolved by setting a parameter on send_command
			so that it can send without reading in those situations. This test makes sure deletion doesn't raise an exception.

			What's unfortunate is that due to the way tthe exception is raised (during a __del__ call), it doesn't actually
			propagate up. So, this test will always pass, but is here so that should an exception occur during object
			deletion, it points back to this test.
		:return:
		"""
		try:
			del self.ctd
		except TypeError:
			self.assertFalse(True)  # raised AttributeError on deleting CTD object
		except:
			self.assertFalse(True)  # raised some exception on deleting CTD object

		self.assertFalse(False)  # does not raise an exception upon deleting CTD object

	def test_data_reconnect(self):
		# we should run a status command, then wait 3 minutes for it to go to sleep, then run it again and make sure we
		# get the results of the command.
		pass

class BaseCTDTest(unittest.TestCase):  # this could be subclassed for specific units - especially setup with baud settings, etc
	def setUp(self):
		self.raw_ctd_file = open(os.path.join(os.path.split(os.path.abspath(__file__))[0], "test_dumps", "SBE19plusv1.log"), 'wb')
		self.ctd = seabird_ctd.CTD(local_test_settings.COM_PORT, baud=local_test_settings.BAUD_RATE, send_raw=self.raw_ctd_file)

	def test_autosample(self):
		#self.ctd.stop_autosample()
		#self.ctd.set_datetime()
		self.got_a_record = False  # we need this so that we can fail if no records were returned at all, or if the records don't have tdata

		def handler(records):
			for record in records:
				self.got_a_record = True
				self.assertLess(float(record["temperature"]), 100)
				self.assertGreater(float(record["temperature"]), -7.5)  # use a temperature bounds check as a way to check for data

				self.assertLess(float(record["pressure"]), 30)  # this assumes we're not testing against a pressure device that's very deep
				self.assertGreater(float(record["pressure"]), -7.5)

				# it's possible we'd want to confirm we have these, but we should force it in the test code, not the CTD code, because parsing failures could lead us astray
				self.assertLess(float(record["salinity"]), 100)  # Ocean salinity around 34 ppm - 100 is to give it a lot of room for testing elsewhere
				self.assertGreater(float(record["salinity"]), -0.01)

				self.assertLess(float(record["conductivity"]), 10)
				self.assertGreater(float(record["conductivity"]), 0)

		self.ctd.start_autosample(30, handler=handler, no_stop=False, max_iterations=4)

		self.assertTrue(self.got_a_record)

	def __del__(self):
		self.raw_ctd_file.close()

if __name__ == "__main__":
	unittest.main()