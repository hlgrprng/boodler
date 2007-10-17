# Boodler: a programmable soundscape tool
# Copyright 2002-7 by Andrew Plotkin <erkyrath@eblong.com>
# <http://eblong.com/zarf/boodler/>
# This program is distributed under the LGPL.
# See the LGPL document, or the above URL, for details.

import sys
import re
import logging
import traceback
import bisect
import StringIO

### get rid of [, var...] in doc comments
### clean up everything that says "watch", "event_reg", "EventAgent"

class Generator:
	"""Generator: A class that stores the internal state of boodler
	sound generation.

	Everything in this class is private to boodler.

	"""

	def __init__(self, basevolume=0.5, dolisten=False, listenport=None):
		self.logger = logging.getLogger()
		self.logger.info('generator setting up')

		self.queue = []
		self.channels = {}
		self.stoplist = []
		self.postqueue = []
		self.allhandlers = {}
		self.listener = None  ### list of listeners?
		self.lastunload = 0
		self.verbose_errors = False
		self.stats_interval = None
		self.statslogger = None
		###if dolisten:
		###	lfunc = lambda val, gen=self: receive_event(gen, val)
		###	self.listener = listen.Listener(lfunc, listenport)

		self.rootchannel = Channel(None, self, None, basevolume, None)

	def close(self):
		if (self.listener):
			self.listener.close()
		self.logger.info('generator shut down')

	def set_stats_interval(self, val):
		self.statslogger = logging.getLogger('stats')
		self.stats_interval = val
		self.last_stats_dump = 0

	def addagent(self, ag, chan, runtime):
		if (ag.queued):
			raise ScheduleError('"' + ag.getname() + '"' + ' is already scheduled')

		ag.generator = self
		ag.runtime = runtime
		ag.channel = chan
		chan.agentcount += 1
		ag.queued = True

		bisect.insort(self.queue, ag)

		ag.logger.info('scheduled on %s', chan)

	def remagent(self, ag):
		ag.logger.info('unscheduled')
		
		ag.queued = False
		ag.channel.agentcount -= 1
		self.queue.remove(ag)

	def addhandler(self, han):
		ag = han.agent
		ag.logger.info('listening for "%s" on %s', han.event, han.listenchannel)

		han.alive = True
		self.allhandlers[han] = han
		ag.handlers[han] = han

		chan = han.listenchannel
		chan.listenhandlers[han] = han
		if (han.holdlisten):
			chan.listenholds += 1

		chan = han.runchannel
		chan.runhandlers[han] = han
		if (han.holdrun):
			chan.runholds += 1

	def remhandlers(self, hans):
		for han in hans:
			# The list may have duplicates, so we have to be careful
			# not to kill a handler twice.
			if (not han.alive):
				continue

			ag = han.agent
			ag.logger.info('stopped listening for "%s"', han.event)
			
			han.alive = False
			self.allhandlers.pop(han)
			ag.handlers.pop(han)
				
			chan = han.listenchannel
			chan.listenhandlers.pop(han)
			if (han.holdlisten):
				chan.listenholds -= 1
				if (chan.listenholds < 0):
					raise BoodleInternalError('channel listenholds negative')
					
			chan = han.runchannel
			chan.runhandlers.pop(han)
			if (han.holdrun):
				chan.runholds -= 1
				if (chan.runholds < 0):
					raise BoodleInternalError('channel runholds negative')

			han.finalize()
		
	def dump_stats(self, fl=None):
		if (fl is None):
			fl = sys.stdout
		write = fl.write

		write('...\n')
		numagent = len(self.queue)
		write('%d agents\n' % (numagent,))
		numchan = len(self.channels)
		write('%d channels\n' % (numchan,))
		numhan = len(self.allhandlers)
		if (numhan):
			write('%d handlers\n' % (numhan,))
		numsamp = len(sample.cache)
		numsamploaded = 0
		numsampunloaded = 0
		numsampvirt = 0
		numnotes = 0
		for samp in sample.cache.values():
			numnotes = numnotes + samp.refcount
			if (samp.csamp is None):
				numsampvirt += 1
			elif (cboodle.is_sample_loaded(samp.csamp)):
				numsamploaded += 1
			else:
				numsampunloaded += 1
		write('%d samples (%d loaded, %d unloaded, %d virtual)\n'
			% (numsamp, numsamploaded, numsampunloaded, numsampvirt))
		write('%d notes\n' % (numnotes,))

class Channel:
	"""Channel: a class for creating hierarchical trees of sounds and
	agents.

	Channel objects should be created via Agent.new_channel() and
	destroyed with the channel.stop() method.

	Public methods and fields:

	parent -- the parent of the channel (None if root)
	get_root_channel() -- return the root channel of the tree
	set_volume() -- change the volume of the channel
	stop() -- stop the channel immediately
	get_prop() -- get a property from this channel
	has_prop() -- see whether this channel has a given property
	set_prop() -- set a property on this channel
	del_prop() -- delete a property from this channel

	Class method:

	compare() -- compare two channels in deepest-to-root order
	"""

	logger = None
	ordinal = 0   # only for display name

	def __init__(self, parent, gen, createagent, startvol, pan):
		self.active = True
		self.generator = gen
		if (not Channel.logger):
			Channel.logger = logging.getLogger('channel')
		Channel.ordinal += 1
		self.ordinal = Channel.ordinal
		self.volume = (0, 0, startvol, startvol)
		self.stereo = stereo.cast(pan)
		self.lastvolume = startvol
		self.notecount = 0
		self.agentcount = 0
		self.childcount = 0
		self.listenhandlers = {}
		self.listenholds = 0
		self.runhandlers = {}
		self.runholds = 0
		self.propmap = {}
		self.parent = parent
		
		if (parent is None):
			self.depth = 0
			self.ancestors = {}
			self.rootchannel = self
		else:
			self.depth = parent.depth+1
			parent.childcount += 1
			self.ancestors = parent.ancestors.copy()
			self.ancestors[parent] = parent
			self.rootchannel = parent.rootchannel
			
		if (createagent is None):
			self.creatorname = '<boodler>'
		else:
			### use the Boodler package name here, if possible
			self.creatorname = createagent.__class__.__name__
			
		gen.channels[self] = self
		self.logger.info('opened %s', self)

	def __str__(self):
		return ('#%d (depth %d, out of %s)' 
			% (self.ordinal, self.depth, self.creatorname))

	def close(self):
		# This is called both from the explicit stop list, and from the
		# regular check for channels with no more stuff scheduled.
		
		if (not self.active):
			return
			
		if (self.childcount > 0):
			raise BoodleInternalError('channel has children at close')
		if (self.agentcount > 0):
			raise BoodleInternalError('channel has agents at close')
		if (self.listenholds > 0):
			raise BoodleInternalError('channel has listens at close')
		if (self.listenhandlers):
			raise BoodleInternalError('channel has listenhandlers at close')
		if (self.runhandlers):
			raise BoodleInternalError('channel has runhandlers at close')
		if (self.notecount > 0):
			raise BoodleInternalError('channel has notes at close')
		if (self.parent):
			self.parent.childcount -= 1
			if (self.parent.childcount < 0):
				raise BoodleInternalError('channel childcount negative')
				
		self.logger.info('closed %s', self)
		gen = self.generator
		self.active = False
		self.generator = None
		self.listenhandlers = None
		self.runhandlers = None
		self.depth = None
		self.ancestors.clear()
		self.ancestors = None
		self.parent = None
		del gen.channels[self]

	def stop(self):
		"""stop()

		Stop the channel immediately. All sounds playing in the channel 
		(or any subchannels) are cut off; all sounds and agents scheduled
		to run are discarded.

		If any notes are playing with non-zero volume, their termination
		may cause undesirable clicks and pops. It is best to set the volume
		of a channel to zero before stopping it. (The FadeOutAgent class
		can be used for this.)

		Due to the way sound generation is buffered, when an agent calls
		channel.stop(), the channel may be stopped slightly later than
		it ought to be.

		"""
		self.generator.stoplist.append(self)

	def realstop(self):
		if (not self.active):
			raise ChannelError('cannot stop an inactive channel')
		gen = self.generator
		
		cboodle.stop_notes(self)
		
		agents = [ ag for ag in gen.queue
			if (ag.channel is self or ag.channel.ancestors.has_key(self)) ]
		for ag in agents:
			gen.remagent(ag)

		hans = [ han for han in gen.allhandlers
			if ((han.runchannel is self
					or han.runchannel.ancestors.has_key(self))
				or (han.listenchannel is self
					or han.listenchannel.ancestors.has_key(self))) ]
		gen.remhandlers(hans)
			
		chans = [ ch for ch in gen.channels
			if (ch is self or ch.ancestors.has_key(self)) ]
		chans.sort(Channel.compare)
		for ch in chans:
			ch.close()

	def addnote(self):
		self.notecount = self.notecount + 1

	def remnote(self):
		self.notecount = self.notecount - 1
		if (self.notecount < 0):
			raise BoodleInternalError('channel notecount negative')

	def get_root_channel(self):
		"""get_root_channel() -> channel

		Return the root channel of the tree.

		"""
		return self.rootchannel

	def set_volume(self, newvol, interval=0.005):
		"""set_volume(newvolume [, interval=0.005])

		Change the volume of the channel to a new level (0 means silence,
		1 means full volume). This affects all notes in the channel and
		any subchannels.

		The volume change begins immediately, and occurs smoothly over
		the interval given (in seconds). If no value is given, the interval
		defaults to 0.005 (five milliseconds), which is short enough that
		it will sound instantaneous. (You should not use an interval
		shorter than 0.005; it can cause undesirable clicks and pops.)

		Due to the way the volume code is written (a cheap and dirty hack),
		two volume changes scheduled too close together on the same channel
		(within about one second) can interfere with each other. The earlier
		one may be ignored entirely in favor of the later. Therefore, you
		should not rely on rapid sequences of set_volume() calls for your
		sound effects. Set volumes on individual notes instead, or else
		create several channels.

		"""
		starttm = self.generator.agentruntime
		endtm = starttm + int(interval * cboodle.framespersec())
		if (endtm >= self.volume[1]):
			self.volume = (starttm, endtm, self.lastvolume, newvol)

	def get_prop(self, key, default=None):
		"""get_prop(key, default=None) -> any

		Get a property from this channel. If none is set, see if one is
		inherited from the parent. If there is no inherited value either,
		return None, or the given default.

		Note that None is a legal property value. To distinguish between
		no property and a property set to None, use has_prop().
		"""
		
		key = check_prop_name(key)
		chan = self
		while (chan):
			if (chan.propmap.has_key(key)):
				return chan.propmap[key]
			chan = chan.parent
		return default
			
	def has_prop(self, key):
		"""has_prop(key) -> bool

		See whether this channel has a given property. If none is set, see
		if one is inherited from the parent.
		"""
		
		key = check_prop_name(key)
		chan = self
		while (chan):
			if (chan.propmap.has_key(key)):
				return True
			chan = chan.parent
		return False
			
	def set_prop(self, key, val):
		"""set_prop(key, val) -> None

		Set a property on this channel.
		"""
		
		key = check_prop_name(key)
		self.propmap[key] = val
			
	def del_prop(self, key):
		"""del_prop(key) -> None

		Delete a property from this channel. If none is set, this has no
		effect.

		Note that this does not affect parent channels. So get_prop(key)
		may still return a value after del_prop(key).
		"""

		key = check_prop_name(key)
		if (self.propmap.has_key(key)):
			self.propmap.pop(key)
			
	def compare(ch1, ch2):
		"""compare(ch1, ch2) -> int

		Compare two channels in depth order. Sorting a list of channels
		with this comparison function will put the deepest ones first,
		the root last.
		"""
		return cmp(ch2.depth, ch1.depth)
	compare = staticmethod(compare)

# Regular expression for valid event/property names: one or more elements,
# separated by periods. Each element must contain only letters, digits,
# and underscores. An element may not start with a digit.
prop_name_regexp = re.compile('\\A[a-zA-Z_][a-zA-Z_0-9]*(\.([a-zA-Z_][a-zA-Z_0-9]*))*\\Z')

# A cache of valid event/property names. We keep this so that we don't
# have to regexp them every time.
valid_prop_names = {}

def check_prop_name(val):
	"""check_prop_name(val) -> str

	Ensure that the value is a valid event or property name. If it isn't, 
	raise BoodlerError. If it is, return a str version of it (in case it 
	was a unicode object).
	"""
	
	res = valid_prop_names.get(val)
	if (res):
		return res
	res = prop_name_regexp.match(val)
	if (not res):
		raise BoodlerError('invalid prop/event name: ' + val)
	res = str(val)
	valid_prop_names[res] = res
	return res
	
TRIMTIME   = 317520000   # two hours
TRIMOFFSET = 158760000   # one hour
UNLOADTIME =  13230000   # five minutes
UNLOADAGE  = 100000000   # 40-ish minutes

####
TRIMTIME   =  80000
TRIMOFFSET =  50000
UNLOADTIME =  50000
UNLOADAGE  = 110000

def run_agents(starttime, gen):
	if (starttime >= TRIMTIME):
		starttime = starttime - TRIMOFFSET
		cboodle.adjust_timebase(TRIMOFFSET)
		gen.lastunload = gen.lastunload - TRIMOFFSET
		sample.adjust_timebase(TRIMOFFSET, UNLOADAGE)
		for ag in gen.queue:
			ag.runtime = ag.runtime - TRIMOFFSET
		for chan in gen.channels:
			(starttm, endtm, startvol, endvol) = chan.volume
			if (endtm <= starttime):
				continue
			starttm = starttm - TRIMOFFSET
			endtm = endtm - TRIMOFFSET
			chan.volume = (starttm, endtm, startvol, endvol)
		if (not (gen.stats_interval is None)):
			gen.last_stats_dump = gen.last_stats_dump - TRIMOFFSET

	if (gen.lastunload + UNLOADTIME < starttime):
		gen.lastunload = starttime
		sample.unload_unused(starttime-UNLOADAGE)

	if (not (gen.stats_interval is None)):
		if (gen.last_stats_dump + int(gen.stats_interval * cboodle.framespersec()) < starttime):
			gen.last_stats_dump = starttime
			fl = StringIO.StringIO()
			gen.dump_stats(fl)
			gen.statslogger.warning(fl.getvalue())
			fl.close()

	nexttime = starttime + cboodle.framesperbuf()

	if (gen.stoplist):
		for chan in gen.stoplist:
			if (chan.active):
				chan.realstop()
		gen.stoplist = []

	if (gen.listener):
		gen.listener.poll()

	gen.agentruntime = starttime
	### this is now only externally posted events; rejigger?
	while (len(gen.postqueue) > 0):
		(ag, ev) = gen.postqueue.pop(0)
		ag.logger.info('running on %s', ev)
		try:
			if (not ag.channel.active):
				raise BoodleInternalError('posted agent not in active channel')
			ag.receive(ev)
		except Exception, ex:
			ag.logger.error('"%s" %s: %s',
				ag.getname(), ex.__class__.__name__, ex,
				exc_info=True)

	while (gen.queue and gen.queue[0].runtime < nexttime):
		ag = gen.queue.pop(0)
		ag.queued = False
		ag.channel.agentcount -= 1
		ag.logger.info('running')
		try:
			if (not ag.channel.active):
				raise BoodleInternalError('queued agent not in active channel')
			gen.agentruntime = ag.runtime
			ag.run()
		except Exception, ex:
			ag.logger.error('"%s" %s: %s',
				ag.getname(), ex.__class__.__name__, ex,
				exc_info=True)
		ag.firsttime = False

	for chan in gen.channels:
		(starttm, endtm, startvol, endvol) = chan.volume
		if (nexttime >= endtm):
			chan.lastvolume = endvol
		elif (nexttime >= starttm):
			chan.lastvolume = (nexttime - starttm) / float(endtm - starttm) * (endvol - startvol) + startvol
		else:
			chan.lastvolume = startvol

	ls = [ chan for chan in gen.channels
		if (chan.notecount == 0
			and chan.agentcount == 0
			and chan.listenholds == 0
			and chan.runholds == 0
			and chan.childcount == 0)
	]
	for chan in ls:
		if (chan.runhandlers):
			gen.remhandlers(list(chan.runhandlers))
		if (chan.listenhandlers):
			gen.remhandlers(list(chan.listenhandlers))
		chan.close()

	if (not gen.channels):
		raise StopGeneration()


### redo (as Generator method)
def receive_event(gen, val):
	if (type(val) in [string, unicode]):
		event = tuple(val.split())
	else:
		event = tuple(val)
	if (len(event) == 0):
		return
	watchers = gen.event_registry.get(event[0])
	if (not watchers):
		return
	for ag in watchers:
		gen.postqueue.append((ag, event))


# Late imports.

import boodle
from boodle import stereo, sample, listen
from boodle import BoodlerError, StopGeneration
# cboodle may be updated later, by a set_driver() call.
cboodle = boodle.cboodle

class ScheduleError(BoodlerError):
	"""ScheduleError: Represents an invalid use of the scheduler.
	"""
	pass
class ChannelError(BoodlerError):
	"""ChannelError: Represents an invalid use of a channel.
	"""
	pass
class BoodleInternalError(BoodlerError):
	"""BoodleInternalError: Represents an internal sanity check going
	wrong.
	"""
	pass
