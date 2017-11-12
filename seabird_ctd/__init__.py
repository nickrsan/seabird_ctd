"""
	SeaBird CTD is meant to handle reading data from a stationary CTD logger. Many packages exist for profile loggers,
	but no options structured for use on a stationary logger or reading. This package is designed to remain directly connected
	to a CTD while it's logging and can be extended to support communication with different versions of the Seabird CTD
	loggers with different capabilities and command descriptions.
"""

__author__ = "nickrsan"
from seabird_ctd.version import version as __version__

import time
from datetime import timezone
import pytz
import datetime
import os
import re
import logging
import traceback

import six

import serial

#logging.basicConfig(level=logging.DEBUG)

try:
	import pika
except ImportError:
	interrupt = None
	logging.debug("pika not available, can't use RabbitMQ to check for messages")

try:
	from seabird_ctd import interrupt
	from multiprocessing import Process
except:
	interrupt = None
	logging.warning("Unable to load a required module for interrupt queue. Can be ignored if not using RabbitMQ, but this message is unusual regardless.")

class SBE37S(object):
	"""
		Handles the SBE37S commands
	"""
	def __init__(self, main_ctd):
		self.max_samples = 3655394  # needs verification. This is just a guess based on SBE39
		self.keys = ("pressure", "conductivity", "temperature", "salinity", "datetime")
		self.ctd = main_ctd

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

class SBE39(object):
	def __init__(self, main_ctd):
		self.max_samples = 3655394
		self.keys = ("temperature", "pressure", "datetime")
		self.ctd = main_ctd

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

supported_ctds = {
	"SBE37S": "SBE37S",
	"SBE 39": "SBE39"
}  # name the instrument will report, then class name. could also do this with 2-tuples.

class CTDConfigurationError(BaseException):
	pass

class CTDOperationError(BaseException):
	pass

class CTDConnectionError(BaseException):
	pass

class CTDUnsupportedError(BaseException):
	pass


class CTD(object):
	def __init__(self, COM_port=None, baud=9600, timeout=5, setup_delay=2, wait_numerator=200):
		"""
		If COM_port is not provided, checks for an environment variable named SEABIRD_CTD_PORT. Otherwise raises
		CTDConnectionError

		"""

		self.setup_complete = False  # we'll use this if setup fails so we can appropriately close the object. If it fails before it's complete, we won't send a QS

		if COM_port is None:
			if "SEABIRD_CTD_PORT" in os.environ:
				COM_port = os.environ["SEABIRD_CTD_PORT"]
			else:
				raise CTDConnectionError("SEABIRD_CTD_PORT environment variable is not defined. Don't know what COM port"
										 "to connect to for CTD data. Can't collect CTD data.")

		self.log = logging.getLogger("seabird_ctd.{}".format(COM_port))  # lets you tune into all seabird_ctd logging, or just this one by com port

		self.last_sample = None
		self.is_sampling = None  # starts at None, will be set when DS is run.
		self.sample_number = None  # will be set by DS command
		self.com_port = COM_port

		self.ctd = serial.Serial(COM_port, baud, timeout=timeout)
		self.baud = baud
		self.timeout = timeout
		self.read_safety_delay = wait_numerator/self.baud
		# read_safety_delay is kind of a weird one. We only read as many characters as we know are on the pipe in order to avoid having to wait for the
		# timeout on every read - which significantly, and unnecessarily prolongs reads. *But* when we're reading while data is being transferred, we
		# read quite a bit faster than data is transferred in, so we can be falsely told that there aren't any characters waiting, only because they're
		# still coming in on the serial line. So, after each read, we have a short sleep of 120/baudrate so that there is time for a new character to
		# register as being available and the read code decides to do another read. The value 120/baudrate isn't *super* scientific and might be able
		# to be refined. Theoretically, at a minimum, we'd need 8/baudrate seconds to receive a full byte of information on the line. I was going to
		# just double it for a safe margin, but that wasn't enough. We still got mixed up commands and responses even as high as 50/baudrate. So, I
		# doubled that, and added a bit and it seems to work reliably. If there are still problems/race conditions around reading data, upping the numerator here
		# may help solve them. Note that even for the slowest baudrates, this is still shorter than waiting for the timeout to hit because we wait
		# the minimum amount of time to ensure no new data is currently being transmitted.
		
		try:
			time.sleep(setup_delay)  # give it time to init

			self.command_object = None
			self.handler = None  # will be set if self.listen is called
			self.held_records = []

			self.model = None  # to be set later

			# STATUS INFO TO BE SET BY .status()
			self.full_model = None
			self.serial_number = None
			self._battery_voltage = None  # actual battery voltage is function so we can keep track of changes
			self._original_voltage = None
			self.lithium_voltage = None
			self.sample_number = None
			self.is_sampling = None
			self.salinity_output = None

			# INTERNAL FLAGS AND INFO
			self.rabbitmq_server = None
			self._stop_monitoring = False  # a flag that will be monitored to determine if we should stop checking for commands
			self._close_connection = False  # same as previous

			self.last_status = None  # will be set each time status is run
			self.determine_ctd_model()
			if self.last_status is None:  # we check it here because determine_ctd_model might run it if it needs to. Don't waste time doubling up.
				self.status()  # will fill some fields in so we know what sample it's on, etc
		except:  # if ANY exception occurs through here, close the ctd object and then reraise the exception so that we're at a clean state
			self.ctd.close()
			raise
		self.setup_complete = True

	@property
	def battery_voltage(self):
		return self._battery_voltage

	@battery_voltage.setter
	def battery_voltage(self, new_value):
		if self._battery_voltage is None:
			self._original_voltage = float(new_value)

		self._battery_voltage = float(new_value)

	@property
	def battery_change(self):
		return (self._battery_voltage - self._original_voltage) / self._original_voltage

	def determine_ctd_model(self):
		try:
			self.log.debug("Waking CTD")
			self.wake()
			ctd_info = self._read_all() # TODO: Is the data returned from wake always junk? What if this is starting up after setting the CTD to autosample, then the server crashes and is reconnecting
		except UnicodeDecodeError:
			raise CTDConfigurationError("Unable to decode response from CTD. Try a different baud setting and ensure it matches the CTD's internal configuration")

		self.log.debug("CTD responded with {}".format(ctd_info))

		for line in ctd_info:  # it should write out the model when you connect - this works unless it hasn't timed out since last connection
			if line in supported_ctds.keys():
				self.log.info("Model Found: {}".format(line))
				self.command_object = globals()[supported_ctds[line]](main_ctd=self)  # get the object that has the command info for this CTD
				self.model = line

		if not self.command_object:  # if we didn't get it from startup, issue a DS to determine it and peel it off the first part
			self.log.debug("Trying backup method to determine model")
			ds = self.send_command("DS")
			self.log.debug("CTD responded with {}".format(ds))
			self.model = ds[1][:6]  # it'll be the first 6 characters of the
			self.log.debug("Best guess for model is {}".format(self.model))

			if self.model not in supported_ctds:
				raise CTDUnsupportedError("Model '{}' is not supported. If this doesn't match the model you have, then the model information did not correctly parse. You may try again.".format(self.model))

			self.command_object = globals()[supported_ctds[self.model]](main_ctd=self)

		if not self.command_object:
			raise CTDConnectionError("Unable to wake CTD or determine its type. There could be a connection error or this is currently plugged into an unsupported model")

	def send_command(self, command=None, length_to_read="ALL"):
		if command:
			self.log.debug("Sending '{}'".format(command))
			self.ctd.write(six.b('{}\r\n'.format(command)))  # doesn't seem to work unless we pass it a windows line ending. Sends command, but no results
			time.sleep(1)  # was this actually necessary? Try removing it.

		self.log.debug("{} bytes in waiting".format(self.ctd.in_waiting))
		if self.ctd.in_waiting > 0:  # if the CTD sent data and we haven't read it yet
			# reads after sending by default so that we can determine if there was a timeout
			if length_to_read == "ALL":
				data = b""
				while self.ctd.in_waiting > 0:
					new_data = self.ctd.read(self.ctd.in_waiting)  # if we're expecting quite a lot, then keep reading until we get nothing - read the exact amount available so it's faster and we don't hit timeout
					data += new_data
					time.sleep(self.read_safety_delay)  # BEFORE CHANGING THIS LINE, check the comments after the definition of read_safety_delay in __init__
			elif length_to_read is None:
				return  # for some commands, such as QS, we want to not attempt a read after sending
			else:
				data = self.ctd.read(length_to_read)

			response = self._clean(data)

			if self.is_sampling:  # any time we read data, if we're sampling, we should check it for records
				self.check_data_for_records(response)

			if "timeout" in response or self.model in response:  # if we got a timeout the first time, then we should be reconnected. Rerun this function and return the results
				# we can safely check for the model because by the time it's all a list, no single line in the list is *only* the model unless we received a timeout
				self.log.debug("Received unexpected response. Resending command (if any - input was {}). Response was {}".format(command, response))
				return self.send_command(command, length_to_read)
			else:
				return response
		else:
			return []  # should return an empty list if there's nothing in waiting

	def _read_all(self):
		return self.send_command(command=None, length_to_read="ALL")

	def _clean(self, response):
		return response.decode("utf-8").split("\r\n")  # first element of list should now be the command, but we'll let the caller filter that

	def set_datetime(self, raise_error=False):
		"""
			Attempt to set the datetime. Logs a warning if that can't be done because the sampler is logging. If you want
			it to raise an exception

		:param raise_error: When False, the default, this function warns if it can't set the datetime. When True, raises CTDOperationError
		:return:
		"""

		if self.is_sampling:
			msg = "Can't change datetime while CTD is sampling. Issue a stop command (or call ctd.stop_autosample()), run set_datetime again, and restart sampling in order to change datetime."
			if raise_error:
				raise CTDOperationError(msg)
			else:
				self.log.warning(msg)
				return

		datetime_commands = self.command_object.set_datetime()
		for command in datetime_commands:
			self.log.info("Setting datetime: {}".format(command))
			self.send_command(command, length_to_read=None)

	def take_sample(self):
		self.last_sample = self.send_command("TS", length_to_read=1000)
		return self.last_sample

	def _filter_samples_to_data(self,):
		pass

	def sleep(self):
		self.send_command("QS", length_to_read=None)

	def wake(self):
		self.send_command("\r\n", length_to_read=None)  # Send a single character to wake the device, get the response so that we clear the buffer

	def status(self):
		try:
			if self.ctd.in_waiting > 0:  # any current waiting characters will make parsing weird.
				self._read_all()  # clear the input buffer, check for any data in the pipeline

			status = self.send_command("DS")
			status_parts = self.command_object.parse_status(status)
			for key in status_parts:  # the command object parses the status message for the specific model. Returns a dict that we'll set as values on the object here
				setattr(self, key, status_parts[key])  # set each returned value as an attribute on this object

			self.last_status = datetime.datetime.now(timezone.utc)

			self.log.info(status)
		except IndexError:
			raise CTDConfigurationError("Unable to parse status message. If this is on a new model of CTD for this package, this may be expected, but needs fixing. Here's what the 'DS' command returned {}".format(status))
		except:  # no matter what exception is raised, we'll most likely want to roll through this
			if self.last_status is not None:  # if this isn't our first check of the status, then warn, but keep going
				self.log.warning("Failed to retrieve or parse status message. Proceeding, but some information, such as battery voltage, may be out of date. CTD response was {}. Error given was {}".format(status, traceback.format_exc()))
			else:  # if it is our first time getting status data, raise the exception up, because we shouldn't proceed without it
				raise

		return status

	def setup_interrupt(self, rabbitmq_server, username, password, vhost, queue=None):
		"""
			Used to set the monitoring functions to use the interrupt method, which allows messages to be passed to the CTD
			from the user even while monitoring for data. By default, if this function is not called, then the monitoring code
			cannot be interrupted and simply runs until the user cancels it.

		:param rabbitmq_server:
		:param username:
		:param password:
		:param vhost:
		:param queue:
		:return:
		"""

		if not interrupt:
			raise CTDConfigurationError("Can't set up interrupt unless Pika Python package is installed. Pika was not found")

		if queue is None:
			queue = self.com_port

		self.rabbitmq_server = rabbitmq_server
		self.interrupt_connection = interrupt.RabbitConsumer(self.rabbitmq_server)
		self.interrupt_connection.username = username
		self.interrupt_connection.password = password
		self.interrupt_connection.vhost = vhost
		self.interrupt_connection.QUEUE = queue

	def check_interrupt(self):
		"""
			Should return True if we're supposed to stop listening or False if we shouldn't

		:return:
		"""

		if not interrupt or not self.rabbitmq_server:  # if pika isn't available, we can't check for messages, so there's always no interrupt
			return False  # these are silently returned False because it means that they didn't try to configure the interrupt

		if self._stop_monitoring:
			return True

		return False  # default is no interrupt

	def start_autosample(self, interval=60, realtime="Y", handler=None, no_stop=False):
		"""
			This should set the sampling interval, then turn on autosampling, then just keep reading the line every interval.
			Before reading the line, it should also check for new commands in a command queue, so it can see if it's
			should be doing something else instead.

			We should do this as an event loop, where we create a celery task to check the line. That way, control can flow
			from here and django can do other work in the meantime. Otherwise, we can have a separate script that does
			the standalone django setup so it can access the models and the DB, or we can just do our own inserts since
			it's relatively simple code here.

		:param interval:  How long, in seconds, should the CTD wait between samples
		:param realtime: Two possible values "Y" and "N" indicating whether the CTD should return results as soon as it collects them
		:param handler: This should be a Python function (the actual object, not the name) that takes a list of dicts as its input.
		 				Each dict represents a sample and has keys for "temperature", "pressure", "conductivity",
		 				and "datetime", as appropriate for the CTD model. It'll skip parameters the CTD doesn't collect.
		 				The handler function will be called whenever new results are available and can do things like database input, etc.
		 				If realtime == "Y" then you must provide a handler function.
		:param no_stop: Allows you to tell it to ignore settings if it's already sampling. If no_stop is True, will just
						start listening to new records coming in (if realtime == "Y"). That way, the CTD won't stop sampling
						for any length of time, but will retain prior settings (ignoring the new interval).
		:return:
		"""

		self.log.info("Initiating autosampling")

		if self.is_sampling and not no_stop:
			self.stop_autosample()  # stop it so we can set the parameters - stop_autosample sets the flag

		if not self.is_sampling:  # will be updated if we successfully stop sampling
			self.send_command(self.command_object.sample_interval(interval), length_to_read=None)  # set the interval to sample at
			self.send_command("TXREALTIME={}".format(realtime), length_to_read=None)  # set the interval to sample at
			self.send_command("STARTNOW", length_to_read="ALL")  # start sampling - we need to read to clear the buffer
			self.send_command("\r\n", length_to_read=None)  # send a newline so that we get a new prompt again
			self.status()  # make sure the status data is up to date. Doing it this way ratehr than setting manually so that if it failed for some reason, the object would still be correct
							# if is_sampling doesn't get updated here, data reading won't work correctly, so this is important

		if realtime == "Y":
			if not handler:  # if they specified realtime data transmission, but didn't provide a handler, abort.
				raise CTDConfigurationError("When transmitting data in realtime, you must provide a handler function that accepts a list of dicts as its argument. See documentation for start_autosample.")
			else:
				self.handler = handler  # make it available to anything else that finds records. Whenever we run any command we need to check for records because it's possible we'll get the output before something else

			self.listen(interval)

	def listen(self, interval=60):
		"""
			Continuously polls for new data on the line at interval. Used when autosampling with realtime transmission to
			receive records.

			See documentation for start_autosample for full documentation of these parameters. This function remains
			part of the public API so you can listen on existing sampling if the autosampler is already configured.

			:param handler: (See start_autosample documentation). A functiion to process new records as they are available.
			:param interval: The sampling interval, in seconds.

			:return:
		"""

		self.log.info("Starting listening loop for CTD data")

		self.check_interval = interval

		def interrupt_handler(ch, method, properties, body):
			"""
				This function is done as a closure because we want it to be able to access attributes of the class instance
				namely, the open pipe to the serial port, as well as knowing which COM port we're on so that it can load
				a RabbitMQ queue with the same name.

				For stopping and closing the connection, this function sets flags on the object that are then responded
				to next time the listen loop wakes up to check the CTD. Flags will be handled before reading the CTD.
				Commands sent to the device will be sent immediately.

				Commands that can be sent to this program are READ_DATA, STOP_MONITORING and DISCONNECT. STOP_MONITORING will just
				stop listening to the CTD, but will leave the CTD in its current autosample configuration.
				If you want stop the CTD from logging, send a Stop command that's appropriate for the CTD model you're
				using (usually STOP) then send a STOP command through the messaging to this program so it will stop checking
				the CTD for new data. DISCONNECT is equivalent to STOP_MONITORING, but also puts the CTD to sleep and
				closes the connection to the CTD.

			:param ch:
			:param method:
			:param properties:
			:param body:
			:return:
			"""

			logging.info(" [x] Received Command via Queue %r" % body)

			body = body.decode("utf-8")
			if body == "READ_DATA":
				if not self._stop_monitoring:
					self.read_records()
			elif body == "DS" or body == "STATUS":
				status = self.status()
				self.log.info(status)
			elif body == "STOP_MONITORING":  # just stop monitoring - once monitoring is stopped, they can connect to the device to stop autosampling
				self.log.info("Stopping monitoring of records")
				self._stop_monitoring = True
				self.interrupt_connection.stop()
			elif body == "DISCONNECT":
				self.log.info("Shutting down monitoring script and closing connection to CTD")
				self.interrupt_connection.stop()
				self.close()  # puts CTD to sleep and closes connection
			else:
				data = self.send_command(body)
				self.log.info(data)

			self.interrupt_connection.acknowledge_message(method.delivery_tag)

		if interrupt and self.rabbitmq_server:
			self.log.debug("Starting interrupt listening loop")

			self.interrupt_connection.handler = interrupt_handler

			self._stop_monitoring = False  # reset the flags that are used to determine if we should stop
			self._close_connection = False

			self.log.debug("Spinning up CTD read loop process")
			self.p = Process(target=interrupt_checker, kwargs={"server": self.rabbitmq_server,
							  "username": self.interrupt_connection.username,
							  "password": self.interrupt_connection.password,
							  "vhost": self.interrupt_connection.vhost,
							  "queue": self.interrupt_connection.QUEUE,
							  "interval": self.check_interval})
			self.p.start()
			self.log.info(self.p.join(5))  # give it 5 seconds of blocking to detect if the process exits

			self.interrupt_connection.run()  # starts listening for commands

		else:  # if no interrupt loop
			while self.check_interrupt() is False:
				try:
					self.read_records()
				except UnicodeDecodeError:
					self.recover()  # UnicodeDecodeError most likely means a line interruption caused invalid data. Try to recover the connection
				time.sleep(interval)

	def read_records(self):

		self.log.debug("Checking for CTD data")

		data = self._read_all()  # this will automatically check for data

		if (datetime.datetime.now(timezone.utc) - self.last_status).total_seconds() > 3600:  # if it's been more than an hour since we checked the status, refresh it so we get new battery stats
			self.wake()
			try:
				self.status()  # this can be run while the device is logging
			except IndexError:
				self.log.warning("Unable to update status information - this is likely fine, but the logger state may not be current.")

	def check_data_for_records(self, data):
		records = self.find_and_insert_records(data)

		if not self.handler:  # if it's sampling and we've received records, but not yet configured a handler, hold onto the records until a handler is configured
			self.held_records += records
		else:
			if len(self.held_records) > 0:
				self.handler(self.held_records)  # handle the held records first, then zero out the list
				self.held_records = []
			self.handler(records)  # Call the provided handler function to do whatever the caller wants to do with the data

	def find_and_insert_records(self, data):
		"""
			Given the parsing regex defined in the command object for this CTD, runs it and returns a list of dicts
			containing sampled values. The dict will always have the keys defined in the command object's "keys" attribute.
			This varies model to model, so your code may need to check for keys, depending. In the event of values that
			are not always enabled (like salinity, or sound velocity), the keys are still present, but set to None if
			those values are not enabled.
		:param data:
		:return:
		"""
		
		self.log.debug("Searching for records in {}".format(data))
		records = []
		for element in data:
			self.log.debug("Trying to match '{}' against regex '{}'".format(element, self.command_object.record_regex()))
			matches = re.search(self.command_object.record_regex(), element)
			if matches is None:
				continue

			new_model = {}
			for key in self.command_object.keys:
				try:
					value = matches.group(key)
					if key == "datetime":
						dt_object = datetime.datetime.strptime(str(value), "%d %b %Y, %H:%M:%S")
						value = pytz.utc.localize(dt_object)
					new_model[key] = value
				except IndexError:
					new_model[key] = None  # it just means that item isn't supported or enabled on this sample

			records.append(new_model)

		return records

	def recover(self):
		"""
			This is for using if the line is interrupted by something, but the script stays running. In our production,
			if power goes out to an intermediate set of devices, the CTD can go down. This method closes the existing
			ctd connection, reopens it, tries to reestablish communication with the device. Triggered if we get a
			UnicodeDecodeError parsing the data.
		:return:
		"""

		self.ctd.close()
		self.ctd = serial.Serial(self.com_port, self.baud, timeout=self.timeout)
		self.wake()
		time.sleep(2)  # give it a moment after trying to recover

	def stop_autosample(self):
		self.log.info("Stopping existing autosampling")
		self.send_command("STOP", length_to_read=None)
		self.status()

		if self.is_sampling is not False:
			raise CTDOperationError("Unable to stop autosampling. If you are starting autosampling, parameters may not be updated")

	def catch_up(self):
		"""
			Meant to pull in any records that are missing on startup of this script - if the autosampler runs in the
			background, then while the script is offline, new data is being stored in flash on the device. This should
			pull in those records.

			NOTE - this command STOPS autosampling if it's running - it must be restarted on its own.

		:return:
		"""

		self.stop_autosample()

		commands = self.command_object.retrieve_samples(0, self.sample_number)
		for command in commands:
			results = self.send_command(command)  # if we have multiple commands, only the later one will have data

		# we now have all the samples and can parse them so that we can insert them.

	def close(self):
		"""
			Put the CTD to sleep and close the connection

		:return:
		"""

		try:
			if self.setup_complete:  # only send a sleep command if setup finished because if it hasn't we won't be able to send the command
				self.sleep()
		except NameError:  # raised when trying to log while closing
			pass  # ignore logging messages during deletion

		self.ctd.close()  # now close the actual serial connection so it's available again
		self.ctd = None  # and zero it out on this object so that if .close() is manually called, tests for if the connection is open work correctly.

	def __del__(self):
		""""
			Check if the CTD is still open. If the user called close on their own, then this will create an error.
		"""
		if hasattr(self, "ctd") and self.ctd:  # if we've already defined the ctd attribute and it's still valid (not manually closed, etc)
			self.close()

def interrupt_checker(server, username, password, vhost, queue, interval):
	"""
		When using the interrupt method, this code handles the scheduling of the actual checking by connecting
		to RabbitMQ and sending READ commands every interval

	:param server:
	:param username:
	:param password:
	:param vhost:
	:param queue:
	:param interval:
	:return:
	"""

	connection = pika.BlockingConnection(pika.ConnectionParameters(host=server, virtual_host=vhost,
																   credentials=pika.PlainCredentials(username, password)))
	channel = connection.channel()

	channel.queue_declare(queue=queue)

	command = "READ_DATA"
	while True:
		channel.basic_publish(exchange='seabird',
						  routing_key='seabird',
						  body=command)

		time.sleep(interval)