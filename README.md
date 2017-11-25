# Seabird CTD
This code is meant to communicate with, control, and read data directly
off of a Seabird CTD. It has been designed for the legacy SBE 39 and the
modern SBE 37+, but can be extended to support other models easily. Other
CTD packages focus on working with the data, especially profiles, once it's downloaded, but this
package is designed to allow for direct control and monitoring during a
longterm fixed deployment. The authors use this code to feed the CTD data
into a database via django in realtime and implement the monitoring as a
management command.

## Installation, Usage, and Documentation
To install, you can use a simple `pip install seabird-ctd` or clone this
repository. You can find the package on PyPI at https://pypi.python.org/seabird-ctd

In addition to the examples below, more complete documentation of the API
can be found at https://seabird_ctd.readthedocs.io.

The following example shows some basic usage of the seabird_ctd code
```python

import seabird_ctd

def handle_records(records):
    """
        This function receives the read data as a list of dicts, and you
        can handle the data however you like in here. In this case, we
        instantiate a new django model (not defined in this example)
        and set its values to match the records received before saving.
    """
	for record in records:
		new_record = CTD()
		new_record.temp = record["temperature"]
		new_record.conductivity = record["conductivity"]
		new_record.pressure = record["pressure"]
		new_record.datetime = record["datetime"]

		new_record.save()

ctd = seabird_ctd.CTD(port="COM6", baud=9600, timeout=5,)  # connect to the device itself over pyserial
if not ctd.is_sampling:  # if it's not sampling, set the datetime to match current device, otherwise, we can't
    ctd.set_datetime()  # this just demonstrates some of the attributes it parses from the status message
else:
    log.info("CTD already logging. Listening in")

ctd.start_autosample(interval=60, realtime="Y", handler=handle_records, no_stop=True)  # start listening in to autosampling results. If it's already sampling, it can stop it, reset parameters, and start it again, or leave it alone, depending on the options you define here
```

## Compatibility
Seabird CTD library is developed for Python 3. Most code should be compatible
with Python 2.7, but has not been tested. If you'd like to make it Python 2.7
compatible, please submit a pull request.

## Simple Setup
This library, with few dependencies, can send commands directly to the device
and monitor and parse responses.

seabird_ctd requires two external packages for simple setup:
* six (for Python 2/3 compatibility)
* pyserial (for communication with the CTD)

After installing those packages and seabird_ctd, you are set up to use the package

When instantiating the CTD object in your code, no arguments are required except the
COM port that the CTD communicates on. If you wish to not specify it in each
script, you can also set an environment variable SEABIRD_CTD_PORT, which will
be checked. Other options are available, including baud rate and the timeout
for reading data (in seconds).

In simple mode, if you initialize autosampling, then the program goes into
a loop that it won't exit from waiting for CTD data. To exit, you will need
to either break/interrupt the program (Ctrl+C) or kill the process.

## Advanced Setup
Advanced setup allows you to send manual commands to the CTD while it is
sampling. It uses a different architecture that listens for commands and
sends those when they are received while still monitoring for data from
samples.

Advanced setup requires significant external setup and small changes to
your code. To use this mode, you must install:

* Erlang (for RabbitMQ)
* RabbitMQ
* Pika python package

You must then set up and configure Rabbit MQ with a virtual host and a user
account that has config privileges in that virtual host.

Then, in your code, prior to initiating autosampling, you call

```python
ctd.setup_interrupt({server_ip_address},
                    {rabbit_mq_username},
                    {rabbit_mq_password},
                    {vhost})
```

Then, you can send commands via rabbit_mq to the queue in the virtual host
that is named by COM port. So, if your CTD runs on COM4, then send messages
to the queue "COM4". Example code, again, as a Django management command,
follows:

```python


class Command(BaseCommand):
	help = 'Sends commands to the CTD while autosampling'

	def add_arguments(self, parser):
		parser.add_argument('--server', nargs='+', type=str, dest="server", default=False,)
		parser.add_argument('--queue', nargs='+', type=str, dest="queue", default=False,)
		parser.add_argument('--command', nargs='+', type=str, dest="command", default=True,)

	def handle(self, *args, **options):
		queue = None
		if options['queue']:
			queue = options['queue'][0]
		else:
			queue = os.environ['SEABIRD_CTD_PORT']

		server = None
		if options['server']:
			server = options['server'][0]
		else:
			server = "192.168.1.253"  # set a default RabbitMQ server if not specified

		command = options['command'][0]  # get the actual command to send

		# connect to RabbitMQ
		connection = pika.BlockingConnection(pika.ConnectionParameters(host=server, virtual_host="moo", credentials=PlainCredentials(local_settings.RABBITMQ_USERNAME, local_settings.RABBITMQ_PASSWORD)))
		channel = connection.channel()

		channel.queue_declare(queue=queue)

		# send the command message
		channel.basic_publish(exchange='seabird',
							  routing_key='seabird',
							  body=command)
		print(" [x] Sent {}".format(command))
		connection.close()
```

You can send any command that the CTD supports executing while autosampling.
See the manual for your CTD for more information on that. You can also send
three special commands. READ_DATA initiates an immediate read of the data
and sends it to the configured handler. STOP_MONITORING leaves the CTD
recording data, but stops the Python code from checking for data.
DISCONNECT is closely related to stop monitoring, but also sends a sleep
command to the device and closes the serial connection.

## Extending the code to a new CTD
CTDs have slightly different command styles, behaviors, and syntaxes and
the authors of this package don't have access to all of the CTDs Seabird
produces. While the package has been tested with a handful of CTDs and
firmware versions (SeacatPlus (SBE19plus), SBE39 firmware 1.5, SBE39 firmware 6,
and SBE37), other models do not have support yet. This code is meant to provide
a structure where it can be extended to support other, newer models, quickly (15 minutes to a few hours).
Until further documentation is written, the best way to see how this is handled is to take a look at
the objects for each CTD in the code in the file ctd_models.py.
They define specific parsing information and command syntax for the CTDs.

## Acknowledgements
This package was developed by [Nick Santos](https://nicksantos.com) for
[Paul Cziko's](https://paulcziko.com) McMurdo Oceanographic Observatory
under contract with the University of Oregon and funded by the National
Science Foundation under grant B-195-M.

## Changelog

### 0.2.4.4
* Fix: SBE39 main now correctly identifies again
* Enhancement: Code will now not send sleep command when device was already put to sleep by code, causing device to wake. Especially useful when code finishes.
* Documentation: More work to flesh out documentation and examples. Still not complete, but slightly better

### 0.2.4.2
* Fix: seabird_ctd would, in certain circumstances, fail to retrieve records from a model that doesn't support commands while logging if that device was logging when it connected

### 0.2.4.1
* New Model: Added support for SBE19Plus - Seacat
* Enhancement: Changed how model is determined to be more robust
* API: Added "operation_wait_times" dict property to command objects to specify wait times for specific commands to complete

### 0.2.4.0
* New Model: Added support for SBE39 firmware version 1.5, which has slightly different behavior from later versions
* API: Added `send_raw` argument to support dumping the entire raw response from the CTD to a file
* API: Added some handlers to command objects to allow them to specify certain behaviors and parsing
* Tests: Added unit test for autologging
* Tests: Began module for simulating outputs of unavailable models for future testing
