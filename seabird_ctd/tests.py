import unittest

import seabird_ctd


class CTDTest(unittest.TestCase):
	def test_data_read(self):

		ctd = seabird_ctd.CTD("COM6")
		try:
			self.assertTrue(hasattr(ctd, "sample_number"))  # this gets assigned after initialization, and comes in when status info is read
		finally:
			ctd.sleep()
			ctd.ctd.close()


	def test_data_reconnect(self):
		# we should run a status command, then wait 3 minutes for it to go to sleep, then run it again and make sure we
		# get the results of the command.