#!/usr/bin/env python3

#>>> client = docker.APIClient(base_url='unix://var/run/docker.sock')
#>>> inspect = client.inspect_container('x-physique-x-b2f513e3b29901e453578b3af8f97568')
#>>> inspect['State']['FinishedAt']
#'2018-10-08T08:05:22.08653639Z'


# pylint: disable=c0111, r0903, r0913, r0914, w1202, w1203, w0703

import os
import time
import calendar
import json
import subprocess
import logging
import re

import asyncio
import aiohttp
from aiohttp import ClientConnectionError

import podman

from nbh_main.settings import sitesettings
# redirect into monitor.log
from nbh_main.settings import monitor_logger as logger
from nbhosting.courses.model_course import CourseDir

from nbhosting.stats.stats import Stats

"""
This processor is designed to be started as a systemd service

It will trigger on a cyclic basis - typically 15 minutes, and will
* spot and kill jupyter instances that have had no recent activity
* when an instance is killed, the stats/<course>/events.raw file
  is updated with a 'killing' line
* also writes into stats/<course>/counts.raw one line with the numbers
  of jupyter instances (running and frozen), and number of running kernels

Also note that

* the notion of 'recent' activity takes advantage of a feature
  introduced in jupyter5, where the /api/kernels/ call returns for
  each kernel its last activity

"""

# not configurable for now
# when we detect an unreachable container, we wait this amount of seconds
# before we decide to kill it, to make sure it is not simply taking off
# makes sense to pick something in the order of magnitude of the 
# global timeout in scripts/nbh
GRACE = 30

class CourseFigures:

    def __init__(self):
        self.frozen_containers = 0
        self.running_containers = 0
        self.running_kernels = 0

    # to avoid counting kernels in containers that get killed
    # we count updateboth counters at the same time
    # of course nb_kernels is meaningful only if running is True
    def count_container(self, running: bool, nb_kernels: int = 0):
        if running:
            self.running_containers += 1
            # nb_kernels may be None
            self.running_kernels += (nb_kernels or 0)
        else:
            self.frozen_containers += 1


class MonitoredJupyter:

    # container is an instance of
    #
    def __init__(self,
                 container: docker.models.containers.Container,
                 course: str,
                 student: str,
                 figures: CourseFigures,
                 # the hash of the expected image - may be None
                 image_hash: str,
                 low_level_api: docker.APIClient):
        self.container = container
        self.course = course
        self.student = student
        self.figures = figures
        self.image_hash = image_hash
        self.low_level_api = low_level_api
        #
        self.nb_kernels = None
        self.last_activity = 0.
        self.inspection = None

    def __str__(self):
        details = f" [{self.nb_kernels}k]" if self.nb_kernels is not None else ""
        return f"container {self.name}{details}"

    # docker containers have a value 'name', so it's confusing
    # to access it through a method
    @property
    def name(self):
        return self.container.name

    def port_number(self):
        try:
            return int(self.container.attrs['NetworkSettings']
                       ['Ports']['8888/tcp'][0]['HostPort'])
        except Exception:
            logger.exception(f"Cannot locate port number for {self}")
            return 0

    @staticmethod
    def parse_docker_time(time_string):
        """
        turn a docker string for a time into an epoch number
        # example #'2018-10-08T08:05:22.08653639Z'
        # sometimes there is no sub-second part
        """
        # normalize
        normalized = time_string.replace('Z', 'UTC')
        # discard all sub-second data
        normalized = re.sub("\.[0-9]+", "", normalized)
        struct_time = time.strptime(normalized, "%Y-%m-%dT%H:%M:%S%Z")
        return calendar.timegm(struct_time)


    @staticmethod
    def last_time(kernel_data):
        """
        expects as input the data returned by /api/kernels
        for one kernel, that is to say e.g.:
        {'connections': 1,
         'execution_state': 'idle',
         'id': '15be5b4c-b5f2-46f0-9a9b-ff54f4495cb4',
         'last_activity': '2018-02-19T12:58:25.204761Z',
         'name': 'python3'}

        returns a comparable time (using max) that this kernel
        has been doing something

        Notes:
        * cases where connections = 0 should not be disregarded
          it is important to keep those alive, it does not indicate
          a lack of activity
        * last_activity format: we found some items where the milliseconds
          part was simply not present (at all, i.e. not exposed as .0 or anything)
        * if anything goes wrong, it's best to return a timestamp that means 'now'
          rather than the epoch
        """
        try:
            last_activity = kernel_data['last_activity']
            return MonitoredJupyter.parse_docker_time(last_activity)
        except Exception:
            logger.exception(f"last_time failed with kernel_data = {kernel_data}")
            # to stay on the safe side, return current time
            return time.time()


    # docker does not seem to expose an asynchronous way to do this
    def inspect(self):
        # run only once
        if self.inspection is None:
            self.inspection = self.low_level_api.inspect_container(self.name)
            
    def creation_time(self):
        self.inspect()
        started_str = self.inspection['State']['StartedAt']
        return self.parse_docker_time(started_str)


    async def count_running_kernels(self):
        """
        updates:
        * self.figures with number of running kernels
        * self.last_activity - a epoch/timestamp/nb of seconds
          may be None if using an old jupyter
        """
        port = self.port_number()
        if not port:
            return
        url = f"http://localhost:{port}/{port}/api/kernels?token={self.name}"
        self.last_activity = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    json_str = await response.text()
            api_kernels = json.loads(json_str)
            self.nb_kernels = len(api_kernels)

            last_times = [
                self.last_time(api_kernel) for api_kernel in api_kernels
            ]
            # if times is empty (no kernel): no activity
            self.last_activity = max(last_times, default=0)

        # this somehow tends to happen a lot sometimes
        # until we figure it out, let's make it less conspicuous
        except ClientConnectionError as exc:
            logger.info(f"{self} could not be reached for last activity, url={url}, {type(exc)}: {exc}")

        except Exception:
            logger.exception(f"Cannot probe number of kernels with {self} - unhandled exception")


    # until 0.23 inclusive, we used to need to remove containers
    # but now we trigger them with the --rm option so it's much simpler
    #
    async def co_run(self, idle, lingering):
        """
        both timeouts in seconds
        """
        now = time.time()
        # ignore containers already marked as to remove
        if self.container.status == 'removing':
            return
        if self.container.status != 'running':
            logger.warning("Ignoring non-running container {self}")
            return
        
        # count number of kernels and last activity
        await self.count_running_kernels()
        # last_activity may be 0 if no kernel is running inside that container
        # or None if we could not determine it properly
        if self.last_activity is None:
            # an unreachable container may be one that is just taking off
            # as unlikely as that sounds, it actually tends to happen much more
            # often than I at least had foreseen at first
            logger.info(f"unreachable (1) {self} - will try again in {GRACE}s")
            await asyncio.sleep(GRACE)
            await self.count_running_kernels()
            if self.last_activity is None:
                logger.info(f"Killing unreachable (2) {self}")
                self.container.kill()
                return
        # check there has been activity in the last grace_idle_in_minutes
        idle_minutes = (int)((now - self.last_activity) // 60)
        if (now - self.last_activity) < idle:
            logger.debug(
                f"Sparing running {self} that had activity {idle_minutes} mn ago")
            self.figures.count_container(True, self.nb_kernels)
        elif self.last_activity == 0:
            logger.info(f"running and empty (1) {self} - will try again in {GRACE}s")
            await asyncio.sleep(GRACE)
            await self.count_running_kernels()
            if self.last_activity == 0:
                logger.info(
                    f"Killing (running and empty) (2) {self} "
                    f"that has no kernel attached")
                self.container.kill()
                return
        else:
            logger.info(
                f"Killing (running & idle) {self} "
                f"that has been idle for {idle_minutes} mn")
            self.container.kill()
            return
        
        # if students accidentally leave stuff running in the background
        # last_activity may be misleading
        # so we kill all caontainers older than <lingering>
        # the unit here is seconds but the front CLI has it in hours
        created_time = self.creation_time()
        ellapsed = int(now - created_time)
        if ellapsed > lingering:
            created_days = (int)(ellapsed // (24 * 3600))
            created_hours = (int)((ellapsed // 3600) % 24)
            logger.warning(
                f"Removing lingering {self} "
                f"that was created {created_days} days "
                f"{created_hours} hours ago (idle_minutes={idle_minutes})")
            self.container.kill()
            return


class Monitor:

    def __init__(self, period, idle, lingering, debug):
        """
        All times in seconds

        Parameters:
          period: is how often the monitor runs
          grace: is how long an idle container is kept running before
            we kill its container (docker kill)
          debug(bool): turn on more logs
        """
        self.period = period
        self.idle = idle
        self.lingering = lingering
        if debug:
            logger.setLevel(logging.DEBUG)


    def run_once(self):
        # initialize all known courses - we want data on all courses
        # even if they don't run any container yet
        logger.debug("scanning courses")
        figures_by_course = {c.coursename : CourseFigures()
                             for c in CourseDir.objects.all()}
        coursedirs_by_name = {c.coursename : c
                              for c in CourseDir.objects.all()}

        try:
            proxy = docker.from_env(version='auto')
            logger.debug("scanning containers")
            containers = proxy.containers.list(all=True)
            hash_by_course = {c.coursename : c.image_hash(proxy)
                              for c in CourseDir.objects.all()}
            low_level_api = docker.APIClient(base_url='unix://var/run/docker.sock')
        except Exception:
            logger.exception(
                "Cannot gather containers list at the docker daemon - skipping")
            return

        # a list of async futures
        futures = []
        for container in containers:
            try:
                # refresh data from docker server, in case
                # we've taken too long to reach here since
                # the point when we issued proxy.containers.list()
                container.reload()
                name = container.name
                # too much spam ven in debug mode
                # logger.debug(f"dealing with container {container}")
                coursename, student = name.split('-x-')
                figures_by_course.setdefault(coursename, CourseFigures())
                figures = figures_by_course[coursename]
                # may be None if s/t is misconfigured
                image_hash = hash_by_course[coursename] \
                       or f"hash not found for course {coursename}"
                monitored_jupyter = MonitoredJupyter(
                    container, coursename, student,
                    figures, image_hash, low_level_api)
                futures.append(monitored_jupyter.co_run(
                    self.idle, self.lingering))
            # typically non-nbhosting containers
            except ValueError:
                # ignore this container as we don't even know
                # in what course it belongs
                logger.info(f"ignoring non-nbhosting {container}")
            except KeyError:
                # typically hash_by_course[coursename] is failing
                # this may happen when a course gets outdated
                logger.info(f"ignoring container {container} - "
                            f"can't find image hash for {coursename}")
            except Exception:
                logger.exception(f"monitor has to ignore {container}")
        # ds stands for disk_space
        docker_root = proxy.info()['DockerRootDir']
        nbhroot = sitesettings.nbhroot
        system_root = "/"
        spaces = {}
        for name, root in (('docker', docker_root),
                           ('nbhosting', nbhroot),
                           ('system', system_root)):
            spaces[name] = {}
            try:
                stat = os.statvfs(root)
                spaces[name]['percent'] = round(100 * stat.f_bfree / stat.f_blocks)
                # unit is MiB
                spaces[name]['free'] = round((stat.f_bfree * stat.f_bsize) / (1024**2))

            except Exception:
                spaces[name]['free'] = 0
                spaces[name]['percent'] = 0
                logger.exception(
                    f"monitor cannot compute disk space {name} on {root}")

        # loads
        try:
            uptime_output = subprocess.check_output('uptime').decode().strip()
            end_of_line = uptime_output.split(':')[-1]
            floads = end_of_line.split(', ')
            load1, load5, load15 = [round(100*float(x)) for x in floads]

        except Exception:
            load1, load5, load15 = 0, 0, 0
            logger.exception(f"monitor cannot compute cpu loads")


        # run the whole stuff
        asyncio.get_event_loop().run_until_complete(
            asyncio.gather(*futures))
        # write results
        for coursename, figures in figures_by_course.items():
            nb_student_homes = coursedirs_by_name[coursename].nb_student_homes()
            Stats(coursename).record_monitor_counts(
                figures.running_containers, figures.frozen_containers,
                figures.running_kernels,
                nb_student_homes,
                load1, load5, load15,
                spaces['docker']['percent'], spaces['docker']['free'],
                spaces['nbhosting']['percent'], spaces['nbhosting']['free'],
                spaces['system']['percent'], spaces['system']['free'],
            )

    def run_forever(self):
        tick = time.time()

        # one cycle can take some time as all the jupyters need to be http-probed
        # so let us compute the actual time to wait
        logger.info("nbh-monitor is starting up")
        for c in CourseDir.objects.all():
            Stats(c.coursename).record_monitor_known_counts_line()
        while True:
            try:
                self.run_once()
            # just be extra sure it doesn't crash
            except Exception:
                logger.exception(f"Unexpected error")
            tick += self.period
            duration = max(0, int(tick - time.time()))
            logger.info(f"monitor is waiting for {duration}s")
            time.sleep(duration)
