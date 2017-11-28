import datetime
import re
from datetime import timezone

class CTDCommandObject(object):
	"""
		Just an empty class that can be subclassed so that any isinstance checks would work
	"""

	operation_wait_times = {}  # keys are for commands - if there is a key for a command here, it waits that many seconds for that operation before trying to read

	def clean_status(self, status):
		"""
			REMINDER: THIS REMOVES *ALL* empty lines, not just leading ones (eg, SBE19plus which has some in the middle)
		:param status:
		:return:
		"""
		remove_lines = ["?cmd S>", "S>", "DS", ""]
		return [line for line in status if line not in remove_lines]  # remove the bad items from the list that confuse the parsers

	def clean_response(self, response):
		return response

	def txrealtime(self, value):
		return "TXREALTIME={}".format(value)  # a smart default for this command


class SBE37S(CTDCommandObject):
	"""
		Handles the SBE37S commands
	"""
	def __init__(self, main_ctd):
		self.max_samples = 3655394  # needs verification. This is just a guess based on SBE39
		self.keys = ("pressure", "conductivity", "temperature", "salinity", "datetime")
		self.ctd = main_ctd
		self.setup()

	def setup(self):
		self.operation_wait_time = 0.5
		self.supports_commands_while_logging = True

	def set_datetime(self):
		dt = datetime.datetime.now(timezone.utc)
		return ["DATETIME={}".format(dt.strftime("%m%d%Y%H%M%S"))]

	def sample_interval(self, interval):
		return "SAMPLEINTERVAL={}".format(interval)

	def retrieve_samples(self, start, end):
		return [
			"OUTPUTFORMAT=1",
			"GetSamples:{},{}".format(start,end)
		]

	def parse_status(self, status_message):
		return_dict = {}
		return_dict["full_model"] = status_message[1].split(" ")[0]  # verified
		return_dict["serial_number"] = status_message[1].split(" ")[4]  # verified

		voltages = re.match("vMain\s+=\s+(\d+\.\d+),\s+vLith\s+=\s+(\d+\.\d+)", status_message[2])
		return_dict["battery_voltage"] = voltages.group(1)  # group zero is whole match, so start with group 1
		return_dict["lithium_voltage"] = voltages.group(2)

		return_dict["sample_number"] = status_message[3].split(", ")[0].split(" = ")[1].replace(" ", "")
		return_dict["is_sampling"] = True if status_message[4] == "logging" else False
		return_dict["salinity_output"] = True if "output salinity" in status_message else False
		return return_dict

	def record_regex(self):
		"""
			Handles generation of the regex for the data records. If salinity output is turned on, handles that correctly.
			STILL TO DO - handle other optional values, such as sound velocity - what happens if sound velocity is on,
			but salinity is off, or if both are on?
		:return:
		"""
		if self.ctd.salinity_output:
			salinity_insert = "\s+(?P<salinity>\d+\.\d+),"
		else:
			salinity_insert = ""

		self.regex = "(?P<temperature>-?\d+\.\d+),\s+(?P<conductivity>-?\d+\.\d+),\s+(?P<pressure>-?\d+\.\d+),"+salinity_insert+"\s+(?P<datetime>\d+\s\w+\s\d{4},\s\d{2}:\d{2}:\d{2})"
		return self.regex


class SBE39(CTDCommandObject):
	def __init__(self, main_ctd):
		self.max_samples = 3655394
		self.keys = ("temperature", "pressure", "datetime")
		self.ctd = main_ctd
		self.setup()

	def setup(self):
		self.operation_wait_time = 0.5
		self.supports_commands_while_logging = True

	def _confirm_model(self, status_message):
		"""
			For models with multiple firmware versions, this takes the results of a DS and returns an activated instance of the correct class
		:return: object
		"""
		model = re.match("(SBE\s?39 .*?)\s+SERIAL NO.*", status_message[1])
		try:
			full_model = model.group(1)
		except:
			self.ctd.log.error("Couldn't match model in status_message: '{}'".format(status_message[0]))
			raise

		if full_model in supported_ctds:  # basically, check if there's a better match based on the full model output in the status message
			return supported_ctds[full_model](self.ctd)
		else:  # if there's not, keep using this one
			return self

	def set_datetime(self):
		dt = datetime.datetime.now(timezone.utc)
		return ["MMDDYY={}".format(dt.strftime("%m%d%y")), "HHMMSS={}".format(dt.strftime("%H%M%S"))]

	def sample_interval(self, interval):
		return "INTERVAL={}".format(interval)

	def retrieve_samples(self, start, end):
		return [
			"DD{},{}".format(start, end)
		]

	def record_regex(self):
		self.regex = "(?P<temperature>-?\d+\.\d+),\s+(?P<pressure>-?\d+\.\d+),\s+(?P<datetime>\d+\s\w+\s\d{4},\s\d{2}:\d{2}:\d{2})"
		return self.regex

	def parse_status(self, status_message):
		return_dict = {}
		return_dict["full_model"] = status_message[1].split("   ")[0]
		return_dict["serial_number"] = status_message[1].split("   ")[1]
		return_dict["battery_voltage"] = status_message[2].split(" = ")[1]
		return_dict["sample_number"] = status_message[5].split(", ")[0].split(" = ")[1]
		return_dict["is_sampling"] = True if status_message[3] == "logging data" else False
		return return_dict


class SBE3915(SBE39):
	"""
		Older SBE39 - firmware 1.5 - similar, but different status parsing
	"""

	def setup(self):
		self.operation_wait_time = 2
		self.max_samples = 230000
		self.supports_commands_while_logging = False

	def parse_status(self, status_message):
		status_message = self.clean_status(status_message)

		if len(status_message) == 0 or status_message[0] != "DS":  # this model seems to get put to sleep after starting autosampling - this is a hackish workaround, but we want to check the status then, so try again
			return None

		return_dict = {}
		model = re.match("(SBE39 [\d\.]+?)\s+SERIAL NO\.\s+(\d+)\s+.*", status_message[1])
		return_dict["full_model"] = model.group(1)  # group zero is whole match, so start with group 1
		return_dict["serial_number"] = model.group(2)
		return_dict["sample_number"] = status_message[4].split(", ")[0].split(" = ")[1]
		return_dict["is_sampling"] = True if status_message[2] == "logging data" else False
		return_dict["battery_voltage"] = None

		return return_dict

	def clean_response(self, response):
		return response.replace(b"\xc5", b"")


class SBE19plus(CTDCommandObject):
	def __init__(self, main_ctd):
		self.max_samples = 727000
		self.keys = ("temperature", "pressure", "conductivity", "salinity", "datetime")
		self.ctd = main_ctd
		self.setup()

	def setup(self):
		self.operation_wait_time = 2
		#self.max_samples = 230000
		self.supports_commands_while_logging = False
		self.operation_wait_times["DS"] = 8

	def set_datetime(self):
		dt = datetime.datetime.now(timezone.utc)
		return ["MMDDYY={}".format(dt.strftime("%m%d%y")), "HHMMSS={}".format(dt.strftime("%H%M%S"))]

	def sample_interval(self, interval):
		return "SAMPLEINTERVAL={}".format(interval)

	def txrealtime(self, value):
		return "MOOREDTXREALTIME={}".format(value)  # a smart default for this command

	def parse_status(self, status_message):
		status_message = self.clean_status(status_message)
		return_dict = {}

		model_parts = re.match("^(.+?)\s+SERIAL\s+NO.\s+(\d+)\s+.*$", status_message[0])
		return_dict["full_model"] = model_parts.group(1)
		return_dict["serial_number"] = model_parts.group(2)

		voltages = re.match("vbatt\s+=\s+(\d+\.\d+),\s+vlith\s+=\s+(\d+\.\d+)", status_message[1])
		return_dict["battery_voltage"] = voltages.group(1)  # group zero is whole match, so start with group 1
		return_dict["lithium_voltage"] = voltages.group(2)

		return_dict["sample_number"] = status_message[6].split(", ")[0].split(" = ")[1].replace(" ", "")
		return_dict["is_sampling"] = True if status_message[4] == "status = logging" else False
		return_dict["salinity_output"] = True if status_message[15].split(" ")[3] == "yes," else False
		return return_dict

	def record_regex(self):
		"""
			Handles generation of the regex for the data records. If salinity output is turned on, handles that correctly.
			STILL TO DO - handle other optional values, such as sound velocity - what happens if sound velocity is on,
			but salinity is off, or if both are on?
		:return:
		"""
		if self.ctd.salinity_output:
			salinity_insert = "\s+(?P<salinity>\d+\.\d+),"
		else:
			salinity_insert = ""

		self.regex = "(?P<temperature>-?\d+\.\d+),\s+(?P<conductivity>-?\d+\.\d+),\s+(?P<pressure>-?\d+\.\d+),.*?"+salinity_insert+"\s+(?P<datetime>\d+\s\w+\s\d{4},\s\d{2}:\d{2}:\d{2})"
		return self.regex


supported_ctds = {
	"SBE37S": SBE37S,
	"SBE 39": SBE39,
	"SBE39": SBE39,  # should be OK to assign to main SBE 39 because it will try to detect if there's a more specific one to use
	"SBE39 1.5": SBE3915,
	"SBE391.5": SBE3915,
	"SeacatPlus V 1.6": SBE19plus,
	"SeacatPlusV1.6": SBE19plus,
	"SeacatPlus": SBE19plus,
}  # name the instrument will report, then class name. could also do this with 2-tuples.