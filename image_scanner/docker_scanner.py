#!/usr/bin/env python
# Copyright (C) 2015 Brent Baude <bbaude@redhat.com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place - Suite 330,
# Boston, MA 02111-1307, USA.

import os
import urllib2
import bz2
import timeit
import argparse
import threading
import logging
import sys
import time
import signal
from image_scanner.dist_breakup import CVEParse
from image_scanner.applicationconfiguration import ApplicationConfiguration
from image_scanner.reporter import Reporter
from image_scanner.scan import Scan
from image_scanner.docker_mount import DockerMount, DockerMountError
from image_scanner.generate_summary import Create_Summary
from image_scanner_client.image_scanner_client import ImageScannerClientError
import subprocess
import psutil
from datetime import datetime
import json
import platform


class Singleton(object):
    ''' Simple Singleton class for variable assignments'''
    _instance = None

    def __new__(cls, *args, **kwargs):
        ''' Creates new instance '''
        if cls._instance is None:
            instance = super(Singleton, cls).__new__(cls)
            instance._singleton_init(*args, **kwargs)
            cls._instance = instance
        return cls._instance

    def __init__(self, *args, **kwargs):
        ''' Fake init def '''
        pass

    def _singleton_init(self, *args, **kwargs):
        """Initialize a singleton instance before it is registered."""
        pass


class ContainerSearch(object):
    ''' Does a series of docker queries to setup variables '''
    def __init__(self):
        self.dead_cids = []
        self.ac = ApplicationConfiguration()
        self.cons = self.ac.conn.containers(all=True)
        self.active_containers = self.ac.conn.containers(all=False)
        self.allimages = self.ac.conn.images(name=None, quiet=False,
                                             all=True, viz=False)
        self.images = self.ac.conn.images(name=None, quiet=False,
                                          all=False, viz=False)
        self.allimagelist = self._returnImageList(self.allimages)
        self.imagelist = self._returnImageList(self.images)
        self.fcons = self._formatCons(self.cons)
        self.fcons_active = self._formatCons(self.active_containers)
        self.ac.fcons = self.fcons
        self.ac.cons = self.cons
        self.ac.allimages = self.allimages
        self.ac.return_json = {}

    def _returnImageList(self, images):
        '''
        Walks through the image list and if the image
        size is not 0, it will add it to the returned
        list.
        '''

        il = []
        for i in images:
            if i['VirtualSize'] > 0:
                il.append(i['Id'])
        return il

    def _formatCons(self, cons):
        '''
        Returns a formatted dictionary of containers by
        image id like:

        fcons = {'iid': [{'cid': {'running': bool}}, ... ]}
        '''
        fcons = {}
        for c in cons:
            cid = c['Id']
            inspect = self.ac.conn.inspect_container(cid)
            iid = inspect['Image']
            run = inspect['State']['Running']
            if 'Dead' in inspect['State']:
                dead = inspect['State']['Dead']
            else:
                dead = False
            if dead:
                self.dead_cids.append(cid)
            if iid not in fcons:
                fcons[iid] = [{'uuid': cid, 'running': run, 'Dead': dead}]
            else:
                fcons[iid].append({'uuid': cid, 'running': run, 'Dead': dead})
        return fcons


class Worker(object):

    min_procs = 2
    max_procs = 4

    def __init__(self, args):
        self.args = args
        self.ac = ApplicationConfiguration(parserargs=args)
        self.procs = self.set_procs(args.number)
        if not os.path.exists(self.ac.workdir):
            os.makedirs(self.ac.workdir)
        self.cs = ContainerSearch()
        self.output = Reporter()
        self.cve_file = os.path.join(self.ac.workdir,
                                     "com.redhat.rhsa-all.xml")
        self.cve_file_bz = os.path.join(self.ac.workdir,
                                        "com.redhat.rhsa-all.xml.bz2")

        self.scan_list = None
        self.rpms = {}
        self.rpms_errors = {}

    def set_procs(self, number):

        numThreads = psutil.NUM_CPUS if number is None else number

        if numThreads < self.min_procs:
            if self.ac.number is not None:
                print "The image-scanner requires --number to be a minimum " \
                      "of {0}. Setting --number to {1}".format(self.min_procs,
                                                               self.min_procs)
            return self.min_procs
        elif numThreads <= self.max_procs:
            return numThreads
        else:
            if self.ac.number is not None:
                print "Due to docker issues, we limit the max number "\
                      "of threads to {0}. Setting --number to "\
                      "{1}".format(self.max_procs, self.max_procs)
            return self.max_procs

    def _get_cids_for_image(self, cs, image):
        cids = []

        if image in cs.fcons:
            for container in cs.fcons[image]:
                cids.append(container['uuid'])
        else:
            for iid in cs.fcons:
                cids = [con['uuid'] for con in cs.fcons[iid]]
                if image in cids:
                    return cids

        return cids

    def get_cve_data(self):

        # FIXME
        # Wrap this in an exception

        hdr = {'User-agent': 'Mozilla/5.0'}
        url = ("http://www.redhat.com/security/data/oval/"
               "com.redhat.rhsa-all.xml.bz2")

        self.ac._print("Obtaining CVE file data from {0}".format(url))

        bar = urllib2.Request(url, "", hdr)
        try:
            resp = urllib2.urlopen(bar)
        except:
            raise ImageScannerClientError("Unable to fetch CVE date from {0}"
                                          .format(url))
        fh = open(self.cve_file_bz, "w")
        fh.write(resp.read())
        fh.close()

    def extract_cve_data(self):
        # Extract the XML bz
        bzfile = bz2.BZ2File(self.cve_file_bz)
        cve_data = bzfile.read()
        open(self.cve_file, 'wb').write(cve_data)

    def return_active_threadnames(self, threads):
        thread_names = []
        for thread in threads:
            thread_name = thread._Thread__name
            if thread_name is not "MainThread":
                thread_names.append(thread_name)

        return thread_names

    def onlyactive(self):
        ''' This function sorts of out only the active containers'''
        con_list = []
        # Rid ourselves of 0 size containers
        for container in self.cs.active_containers:
            con_list.append(container['Id'])
        if len(con_list) == 0:
            error = "There are no active containers on this system"
            if not self.ac.api:
                print error
                sys.exit(1)
            else:
                raise ImageScannerClientError(error)
        else:
            self._do_work(con_list)

    def allimages(self):
        if len(self.cs.imagelist) == 0:
            error = "There are no images on this system"
            if not self.ac.api:
                print error
                sys.exit(1)
            else:
                raise ImageScannerClientError(error)
        if self.args.allimages:
            self._do_work(self.cs.allimagelist)
        else:
            self._do_work(self.cs.imagelist)

    def list_of_images(self, image_list):
        self._do_work(image_list)

    def allcontainers(self):
        if len(self.cs.cons) == 0:
            error = "There are no containers on this system"
            if not self.ac.api:
                print error
                sys.exit(1)
            else:
                raise ImageScannerClientError(error)
        else:
            con_list = []
            for con in self.cs.cons:
                con_list.append(con['Id'])
            self._do_work(con_list)

    def _do_work(self, image_list):
        self.scan_list = image_list
        cp = CVEParse(self.ac.workdir)
        if (not os.path.exists(cp.xmlf)) or \
                (self.ac.nocache) or \
                ((time.time() - os.path.getmtime(cp.xmlf)) / (60 ** 2) > 12):
            # If we find a tarball of the dist break outs and
            # it is less than 12 hours old, use it to speed things
            # up

            self.get_cve_data()
            self.extract_cve_data()

            self.ac._print("Splitting master XML file into distribution "
                           "specific XML files")

            # Run dist breakout to make dist specific XML
            # files
            t = timeit.Timer(cp.parse_for_platform).timeit(number=1)
            logging.debug("Parsed distribution breakup in "
                          "{0} seconds".format(t))
        self.ac._print("\nBegin processing\n")
        threads = []

        for image in image_list:
            if image in self.cs.dead_cids:
                raise ImageScannerClientError("Scan not completed. Cannot "
                                              "scan the dead "
                                              "container {0}".format(image))
            cids = self._get_cids_for_image(self.cs, image)
            t = threading.Thread(target=self.search_containers, name=image,
                                 args=(image, cids, self.output,))
            threads.append(t)

        logging.info("Number of containers to scan: {0}".format(len(threads)))
        total_images = len(threads)
        if isinstance(threading.current_thread(), threading._MainThread):
            signal.signal(signal.SIGINT, self.signal_handler)
        self.threads_complete = 0
        self.ac._print("")
        while len(threads) > 0:
            if len(threading.enumerate()) < self.procs:
                new_thread = threads.pop()
                new_thread.start()
                self._progress(float(self.threads_complete),
                               float(total_images))
        # Seeing some weirdness with the exit thread count
        # when using the API, depends on how it is called

        if self.ac.api:
            exit_thread_count = 1
        else:
            exit_thread_count = 1

        while len(threading.enumerate()) > exit_thread_count:
            self._progress(float(self.threads_complete), float(total_images))
            time.sleep(1)
            pass

        self._progress(float(self.threads_complete), float(total_images))
        self.ac._print("\n" * 2)
        self.output.report_summary()

    def _progress(self, complete, total):
        if not self.ac.api:
            sys.stdout.write("\r[{0:20s}] {1}%    {2}/{3}"
                             .format('#' * int(complete / total * 20),
                                     int(complete / total * 100),
                                     int(complete), int(total)))
            sys.stdout.flush()

    def signal_handler(self, signal, frame):
        print "\n\nExiting..."
        sys.exit(0)

    def search_containers(self, image, cids, output):
        f = Scan(image, cids, output)
        try:
            if f.get_release():
                t = timeit.Timer(f.scan).timeit(number=1)
                logging.debug("Scanned chroot for image {0}"
                              " completed in {1} seconds"
                              .format(image, t))
                timeit.Timer(f.report_results).timeit(number=1)
            else:
                # This is not a RHEL image or container
                f._report_not_rhel(image)
        except subprocess.CalledProcessError:
            pass

        try:
            self.rpms[image] = f._get_rpms()
        except Exception as e:
            self.rpms_errors[image] = str(e)

        start = time.time()
        f.DM.cleanup(f.dm_results)
        logging.debug("Removing temporary chroot for image {0} completed in"
                      " {1} seconds".format(image, time.time() - start))
        self.threads_complete += 1

    def _check_input(self, image_list):
        '''
        Takes a list of image ids, image-names, container ids, or
        container-names and returns a list of images ids and
        container ids
        '''
        dm = DockerMount(dockerclient=self.ac.conn)
        work_list = []
        # verify
        try:
            for image in image_list:
                iid, dtype = dm.get_iid(image)
                work_list.append(iid)
        except DockerMountError:
            error = "Unable to associate {0} with any image " \
                    "or container".format(image)
            if not self.ac.api:
                print error
                sys.exit(1)
            else:
                raise ImageScannerClientError(error)
        return work_list

    def start_application(self):
        start_time = time.time()
        logging.basicConfig(filename=self.ac.logfile,
                            format='%(asctime)s %(levelname)-8s %(message)s',
                            datefmt='%m-%d %H:%M', level=logging.DEBUG)
        try:
            if self.args.onlyactive:
                self.onlyactive()
            if self.args.allcontainers:
                self.allcontainers()
            if self.args.allimages or self.args.images:
                self.allimages()
            if self.args.scan:
                # Check to make sure we have valid input
                image_list = self._check_input(self.args.scan)
                self.list_of_images(image_list)

        except ImageScannerClientError as scan_error:
            if not self.ac.api:
                print scan_error
                sys.exit(1)
            else:
                return {'Error': str(scan_error)}, None
        end_time = time.time()
        duration = (end_time - start_time)
        if duration < 60:
            unit = "seconds"
        else:
            unit = "minutes"
            duration = duration / 60
        logging.info("Completed entire scan in {0} {1}".format(duration, unit))
        if self.ac.api:
            self.dump_json_log()
            return self.ac.return_json, self.ac.json_url

    def _get_rpms_by_obj(self, docker_obj):
        return self.rpms[docker_obj]

    def _get_rpms_error_by_obj(self, docker_obj):
        return self.rpms_errors.get(docker_obj, None)

    def dump_json_log(self):
        '''
        Creates a log of information about the scan and what was
        scanned for post-scan analysis
        '''

        xmlp = Create_Summary()

        # Common Information
        json_log = {}
        json_log['hostname'] = platform.node()
        json_log['scan_time'] = datetime.today().isoformat(' ')
        json_log['scanned_content'] = self.scan_list
        json_log['host_results'] = {}
        json_log['docker_state'] = self.ac.fcons
        json_log['host_images'] = [image['Id'] for image in self.ac.allimages]
        json_log['host_containers'] = [con['Id'] for con in self.ac.cons]
        json_log['docker_state_url'] = self.ac.json_url

        tuple_keys = ['rest_host', 'rest_port', 'allcontainers',
                      'allimages', 'images', 'logfile', 'number',
                      'reportdir', 'workdir', 'api', 'url_root',
                      'host']
        for tuple_key in tuple_keys:
            tuple_val = None if not hasattr(self.ac.parserargs, tuple_key) \
                else getattr(self.ac.parserargs, tuple_key)
            json_log[tuple_key] = tuple_val

        # Per scanned obj information

        for docker_obj in self.scan_list:
            json_log['host_results'][docker_obj] = {}
            tmp_obj = json_log['host_results'][docker_obj]
            tmp_obj['rpms'] = self._get_rpms_by_obj(docker_obj)
            rpms_error = self._get_rpms_error_by_obj(docker_obj)
            if rpms_error is not None:
                tmp_obj['rpmsError'] = rpms_error
            if 'msg' in self.ac.return_json[docker_obj].keys():
                tmp_obj['isRHEL'] = False
            else:
                tmp_obj['isRHEL'] = True
                xml_path = self.ac.return_json[docker_obj]['xml_path']
                tmp_obj['cve_summary'] = \
                    xmlp._summarize_docker_object(xml_path,
                                                  json_log, docker_obj)

        # Pulling out good stuff from summary by docker object
        for docker_obj in self.ac.return_json.keys():
            if 'msg' not in self.ac.return_json[docker_obj].keys():
                for key, value in self.ac.return_json[docker_obj].iteritems():
                    json_log['host_results'][docker_obj][key] = value

        # FIXME
        # Decided to remove this from the JSON as it is considered to be
        # duplicate.  Feel free to remove after some time.

        # json_log['results_summary'] = self.ac.return_json

        # DEBUG
        # print  json.dumps(json_log, indent=4, separators=(',', ': '))
        with open(self.ac.docker_state, 'w') as state_file:
            json.dump(json_log, state_file)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Scan Utility for Containers')
    group = parser.add_mutually_exclusive_group(required=True)

    group.add_argument('--images', help='search images', default=False,
                       action='store_true')
    group.add_argument('--allimages', help='search all images', default=False,
                       action='store_true')
    group.add_argument('--onlyactive', help='search only active containers',
                       default=False, action='store_true')
    group.add_argument('--allcontainers', help='search all containers',
                       default=False, action='store_true')
    group.add_argument('-s', '--scan', help='image to search',
                       action='append')
    parser.add_argument('-n', '--number', help='number of processors to use',
                        type=int, default=None)
    parser.add_argument('-l', '--logfile', help='logfile to use',
                        default="/var/tmp/image-scanner/openscap.log")
    parser.add_argument('-r', '--reportdir', help='directory to store reports',
                        default="/var/tmp/image-scanner")

    parser.add_argument('-w', '--workdir', help='workdir to use, defaults "\
                        "to /var/tmp/image-scanner',
                        default="/var/tmp/image-scanner")
    parser.add_argument('--nocache', default=False, help='Do not cache "\
                        "anything',
                        action='store_true')
    parser.add_argument('-H', '--host', default='unix://var/run/docker.sock',
                        help='Specify docker host socket to use')

    args = parser.parse_args()

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    work = Worker(args)
    work.start_application()
