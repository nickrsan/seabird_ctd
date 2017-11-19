This directory is meant to store raw dumps of the responses from different
CTDs while running autologging tests. In many cases, I lose access to a CTD
(because it's deployed in battery mode), so I can't test to make sure changes
to code work with it. Certain CTDs have specific response quirks,
such as the SBE39 1.5, which inserts a character we can't decode to unicode
when autologging. We want to capture these to make sure we can run tests
against these devices even when we don't have them on hand. Right now,
the facility for *using* these dumps is minimal and untested (see simulated.py),
but shouldn't be too hard to flesh out for basic use cases currently supported
in this package (status information, autologging)

For autologging, one quick note is that when autologging is active, the
code should put the next observation in the read queue at the end of the
previous read so that there are bytes in waiting when the read is checked.
Could maybe do some interval checking, but would need to be careful about that
if don't want to go into infinite "no data" loops.

