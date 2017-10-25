"""
	SeaBird CTD is meant to handle reading data from a stationary CTD logger. Many packages exist for profile loggers,
	but no options structured for use on a stationary logger or reading. This package is designed to remain directly connected
	to a CTD while it's logging and can be extended to support communication with different versions of the Seabird CTD
	loggers with different capabilities and command descriptions.
"""

__author__ = "nickrsan"
import seabird_ctd.version as __version__

import time
from datetime import timezone
import pytz
import datetime
import os
import re
import logging
import six

import serial

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("seabird_ctd")

try:
	import pika
	from seabird_ctd import interrupt
	from multiprocessing import Process
except ImportError:
	interrupt = None
	log.debug("pika not available, can't use RabbitMQ to check for messages")

class SBE37(object):
	def __init__(self):
		self.max_samples = 3655394  # needs verification. This is just a guess based on SBE39
		self.keys = ("temperature", "pressure", "datetime")

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
		return_dict["full_model"] = status_message[1].split(" ")[0]
		return_dict["serial_number"] = status_message[1].split(" ")[4]

		voltages = re.match("vMain\s+=\s+(\d+\.\d+),\s+vLith\s+=\s+(\d+\.\d+)", status_message[2])
		return_dict["battery_voltage"] = voltages.group(0)
		return_dict["lithium_voltage"] = voltages.group(1)

		return_dict["sample_number"] = status_message[3].split(", ")[0].split(" = ")[1].replace(" ", "")
		return_dict["is_sampling"] = True if status_message[4] == "logging data" else False
		return return_dict

class SBE39(object):
	def __init__(self):
		self.max_samples = 3655394
		self.keys = ("temperature", "pressure", "datetime")

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
	"SBE 37": "SBE37",
	"SBE 39": "SBE39"
}  # name the instrument will report, then class name. could also do this with 2-tuples.

class CTDConfigurationError(BaseException):
	pass

class CTDOperationError(BaseException):
	pass

class CTDConnectionError(BaseException):
	pass

class CTD(object):
	def __init__(self, COM_port=None, baud=9600, timeout=5, setup_delay=2):
		"""
		If COM_port is not provided, checks for an environment variable named SEABIRD_CTD_PORT. Otherwise raises
		CTDConnectionError
		"""
		if COM_port is None:
			if "SEABIRD_CTD_PORT" in os.environ:
				COM_port = os.environ["SEABIRD_CTD_PORT"]
			else:
				raise CTDConnectionError("SEABIRD_CTD_PORT environment variable is not defined. Don't know what COM port"
										 "to connect to for CTD data. Can't collect CTD data.")

		self.last_sample = None
		self.is_sampling = None  # starts at None, will be set when DS is run.
		self.sample_number = None  # will be set by DS command
		self.com_port = COM_port

		self.ctd = serial.Serial(COM_port, baud, timeout=timeout)
		time.sleep(setup_delay)  # give it time to init

		self.command_object = None
		self.handler = None  # will be set if self.listen is called

		# STATUS INFO TO BE SET BY .status()
		self.full_model = None
		self.serial_number = None
		self._battery_voltage = None  # actual battery voltage is function so we can keep track of changes
		self._original_voltage = None
		self.lithium_voltage = None
		self.sample_number = None
		self.is_sampling = None

		# INTERNAL FLAGS AND INFO
		self.rabbitmq_server = None
		self._stop_monitoring = False  # a flag that will be monitored to determine if we should stop checking for commands
		self._close_connection = False  # same as previous

		self.determine_ctd_model()
		self.last_status = None  # will be set each time status is run
		self.status()  # will fill some fields in so we know what sample it's on, etc

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
			ctd_info = self.wake()  # TODO: Is the data returned from wake always junk? What if this is starting up after setting the CTD to autosample, then the server crashes and is reconnecting
		except UnicodeDecodeError:
			raise CTDConfigurationError("Unable to decode response from CTD. Try a different baud setting and ensure it matches the CTD's internal configuration")

		for line in ctd_info:  # it should write out the model when you connect
			if line in supported_ctds.keys():
				log.info("Model Found: {}".format(line))
				self.command_object = globals()[supported_ctds[line]]()  # get the object that has the command info for this CTD
				self.model = line

		if not self.command_object:  # if we didn't get it from startup, issue a DS to determine it and peel it off the first part
			self.model = self._send_command("DS")[1][:6]  # it'll be the first 6 characters of the
			self.command_object = globals()[supported_ctds[self.model]]()

		if not self.command_object:
			raise CTDConnectionError("Unable to wake CTD or determine its type. There could be a connection error or this is currently plugged into an unsupported model")

	def _send_command(self, command=None, length_to_read="ALL"):
		if command:
			self.ctd.write(six.b('{}\r\n'.format(command)))  # doesn't seem to work unless we pass it a windows line ending. Sends command, but no results

		# reads after sending by default so that we can determine if there was a timeout
		if length_to_read == "ALL":
			data = b""
			new_data = "start"
			while new_data not in (b"", None):
				new_data = self.ctd.read(1000)  # if we're expecting quite a lot, then keep reading until we get nothing
				data += new_data
		else:
			data = self.ctd.read(length_to_read)

		response = self._clean(data)

		if self.is_sampling:  # any time we read data, if we're sampling, we should check it for records
			self.check_data_for_records(response)

		if "timeout" in response:  # if we got a timeout the first time, then we should be reconnected. Rerun this function and return the results
			return self._send_command(command, length_to_read)
		else:
			return response

	def _read_all(self):
		return self._send_command(command=None, length_to_read="ALL")

	def _clean(self, response):
		return response.decode("utf-8").split("\r\n")  # first element of list should now be the command, but we'll let the caller filter that

	def set_datetime(self):
		datetime_commands = self.command_object.set_datetime()
		for command in datetime_commands:
			log.info("Setting datetime: {}".format(command))
			self._send_command(command)

	def take_sample(self):
		self.last_sample = self._send_command("TS", length_to_read=1000)
		return self.last_sample

	def _filter_samples_to_data(self,):
		pass

	def sleep(self):
		self._send_command("QS")

	def wake(self):
		return self._send_command(" ", length_to_read="ALL")  # Send a single character to wake the device, get the response so that we clear the buffer

	def status(self):
		status = self._send_command("DS")
		status_parts = self.command_object.parse_status(status)
		for key in status_parts:  # the command object parses the status message for the specific model. Returns a dict that we'll set as values on the object here
			setattr(self, key, status_parts[key])  # set each returned value as an attribute on this object

		self.last_status = datetime.datetime.now(timezone.utc)

		log.info(status)

		return status

	def setup_interrupt(self, rabbitmq_server, username, password, vhost, queue=None):

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
		log.info("Initiating autosampling")

		if self.is_sampling and not no_stop:
			self.stop_autosample()  # stop it so we can set the parameters

		if not self.is_sampling:  # will be updated if we successfully stop sampling
			self._send_command(self.command_object.sample_interval(interval))  # set the interval to sample at
			self._send_command("TXREALTIME={}".format(realtime))  # set the interval to sample at
			self._send_command("STARTNOW")  # start sampling

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

		log.info("Starting listening loop for CTD data")

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
				log.info(status)
				self.check_data_for_records(status)  # since logging is running now, make sure we didn't accidentally pull in some data
			elif body == "STOP_MONITORING":  # just stop monitoring - once monitoring is stopped, they can connect to the device to stop autosampling
				log.info("Stopping monitoring of records")
				self._stop_monitoring = True
				self.interrupt_connection.stop()
			elif body == "DISCONNECT":
				log.info("Shutting down monitoring script and closing connection to CTD")
				self.interrupt_connection.stop()
				self.close()  # puts CTD to sleep and closes connection
			else:
				data = self._send_command(body)
				log.info(data)
				self.check_data_for_records(data)

			self.interrupt_connection.acknowledge_message(method.delivery_tag)

		if interrupt and self.rabbitmq_server:
			log.debug("Starting interrupt listening loop")

			self.interrupt_connection.handler = interrupt_handler

			self._stop_monitoring = False  # reset the flags that are used to determine if we should stop
			self._close_connection = False

			log.debug("Spinning up CTD read loop process")
			self.p = Process(target=interrupt_checker, kwargs={"server": self.rabbitmq_server,
							  "username": self.interrupt_connection.username,
							  "password": self.interrupt_connection.password,
							  "vhost": self.interrupt_connection.vhost,
							  "queue": self.interrupt_connection.QUEUE,
							  "interval": self.check_interval})
			self.p.start()
			log.info(self.p.join(5))  # give it 5 seconds of blocking to detect if the process exits

			self.interrupt_connection.run()  # starts listening for commands

		else:  # if no interrupt loop
			while self.check_interrupt() is False:
				self.read_records()
				time.sleep(interval)

	def read_records(self):

		log.debug("Checking for CTD data")

		data = self._read_all()
		log.debug("Data received")
		log.debug(data)
		self.check_data_for_records(data)

		if (datetime.datetime.now(timezone.utc) - self.last_status).total_seconds() > 3600:  # if it's been more than an hour since we checked the status, refresh it so we get new battery stats
			self.status()  # this can be run while the device is logging

	def check_data_for_records(self, data):
		records = self.find_and_insert_records(data)
		self.handler(records)  # Call the provided handler function to do whatever the caller wants to do with the data

	def find_and_insert_records(self, data):
		records = []
		for element in data:
			matches = re.search(self.command_object.record_regex(), element)
			if matches is None:
				continue

			new_model = {}
			if "temperature" in self.command_object.keys:
				new_model["temperature"] = matches.group("temperature")
			else:
				new_model["temperature"] = None

			if "pressure" in self.command_object.keys:
				new_model["pressure"] = matches.group("pressure")
			else:
				new_model["pressure"] = None

			if "conductivity" in self.command_object.keys:
				new_model["conductivity"] = matches.group("conductivity")
			else:
				new_model["conductivity"] = None

			if "datetime" in self.command_object.keys:
				dt_object = datetime.datetime.strptime(str(matches.group("datetime")), "%d %b %Y, %H:%M:%S")
				dt_aware = pytz.utc.localize(dt_object)
				new_model["datetime"] = dt_aware
			else:
				new_model["datetime"] = None

			records.append(new_model)

		return records

	def stop_autosample(self):
		self._send_command("STOP")
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
			results = self._send_command(command)  # if we have multiple commands, only the later one will have data

		# we now have all the samples and can parse them so that we can insert them.

	def close(self):
		"""
			Put the CTD to sleep and close the connection
		:return:
		"""
		self.sleep()
		self.ctd.close()

	def __del__(self):
		self.close()
		pass

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