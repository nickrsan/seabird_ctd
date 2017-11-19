from seabird_ctd import CTDUnsupportedError

class BaseSimulatedCTD(object):
	def __init__(self):
		self.is_sampling = False
		self.realtime = "N"
		self.interval = 60

		self.last_command = None
		self.model = None
		self.full_model = None
		self.response = None

	def setup(self, model=None, full_model=None):
		self.model = model
		self.full_model = full_model

	def write(self, command):
		command = command.replace("\r\n", "")
		if "=" in command:
			method_name, argument = command.split("=")
		else:
			method_name = command
			argument = None

		if hasattr(self, method_name):
			if argument:
				self.response += getattr(self, method_name)(argument)  # if we have that command as a method, call it with the argument
			else:
				self.response += getattr(self, method_name)
		else:
			raise CTDUnsupportedError("That command is not supported on this simulation")

		self.last_command = method_name

	def read(self):
		if self.last_command is None:
			return self.model

		response = self.response  # this value stores up responses until read
		self.response = None
		return b"S>" + self.last_command + "\r\n" + response

	@property
	def in_waiting(self):
		return len(self.response)


class SBE3915(BaseSimulatedCTD):
	def DS(self):
		if self.is_sampling:
			logging = b"logging_data"
		else:
			logging = b"logging not started"  # this doesn't cover all actual cases, but is fine for now

		if self.realtime:
			rto = b"real-time output enabled"
		else:
			rto = b"real-time output disabled"

		return b"SBE39 1.5  SERIAL NO. 0373    19 Nov 2017  00:25:05\n" \
				+logging+ \
				b"sample interval = " + bytes(self.interval) + b"seconds\n" \
				b"samplenumber = 3605, free = 229411\n" \
				b"serial sync mode disabled\n" \
				+ rto + \
				b"SBE 39 configuration = sheathed temperature with pressure" \
				b"temperature = 21.21 deg C"
