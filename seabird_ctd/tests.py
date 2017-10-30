import unittest

import seabird_ctd


class CTDTest(unittest.TestCase):
	def setUp(self):
		self.ctd = seabird_ctd.CTD(baud=4800)

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


if __name__ == "__main__":
	unittest.main()