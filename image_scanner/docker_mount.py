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

"""Docker Mount Python API"""

import os
import collections
import subprocess
import json
import time
import logging


class DockerMountError(Exception):
    """Docker Error"""
    pass


class DockerMount(object):
    """Main DockerMount Class"""
    _mount = collections.namedtuple('mount_tuple',
                                    ['container_id', 'thin_pathname',
                                     'mount_path', 'thin_dev_name',
                                     'dtype'])

    def __init__(self, dockerclient, mnt_point=None, override=False):
        self.conn = dockerclient
        self.override = override
        self.mnt_point = mnt_point
        self.dev_name = None
        self.docker_info = self.conn.info()
        self.driver = self.docker_info['Driver']
        self.return_tuple = collections.namedtuple('returns',
                                                   ['return_code', 'stderr',
                                                    'stdout'])

    def no_docker_backend(self, iid):
        '''
        A work-around for those without vgoyal's docker back-end
        patches.  This will be deprecated as soon as we have
        RHEL patches for it
        '''
        docker_meta = "/var/lib/docker/devicemapper/metadata"
        desc_file = os.path.join(docker_meta, iid)
        desc = json.loads(open(desc_file).read())
        return desc['size'], desc['device_id']

    def get_image_info(self, iid, dtype):
        '''
        Collections information about an image and returns in a
        tuple containing the graph driver, thin_device_id, iid, and
        thin_device_size
        '''

        if dtype is "image":
            image_info = self.conn.inspect_image(iid)
        else:
            image_info = self.conn.inspect_container(iid)

        image_tuple = collections.namedtuple(
            'ImageInfo', ['graphdriver', 'thin_device_id', 'thin_device_size',
                          'iid', 'dtype'])

        # Check if we have the back-end
        if 'GraphDriver' in image_info:

            # Breaking out to comply with Pep8
            thin_device_id = image_info['GraphDriver']['Data'][0][1]
            thin_device_size = image_info['GraphDriver']['Data'][1][1]
            iid = image_info['Id']
            graphdriver = image_info['GraphDriver']['Name']

        # No backend, so will be deprecated
        else:
            graphdriver = None
            thin_device_size, thin_device_id = self.no_docker_backend(iid)

        image_ret = image_tuple(graphdriver=graphdriver,
                                thin_device_id=thin_device_id,
                                thin_device_size=int(thin_device_size)/512,
                                iid=iid,
                                dtype=dtype)
        return image_ret

    def get_cid(self, input_name):
        """
        Given a container name or container id, it will return the
        container id
        """
        containers = self.conn.containers(all=True)
        for container in containers:
            if 'Names' in container and container['Names'] is not None:
                if (container['Id'].startswith(input_name)) or \
                        (('Names' in container) and
                         (any(input_name in item for item in
                              container['Names']))):
                    return container['Id']
                    break
        return None

    def is_device_active(self, device):
        '''
        Checks dmsetup to see if a device is already active
        '''
        cmd = ['dmsetup', 'info', device]
        dmsetup_info = self.subp(cmd)
        for dm_line in dmsetup_info.stdout.split("\n"):
            line = dm_line.split(":")
            if ("State" in line[0].strip()) and ("ACTIVE" in line[1].strip()):
                return self.return_tuple(return_code=0, stderr='',
                                         stdout='DM {0} is already "\
                                                 "active'.format(device))
        return self.return_tuple(return_code=1, stderr='', stdout='')

    def _namesearch(self, input_name):
        """
        Looks to see if the input name is the name of a image
        """
        if ":" in input_name:
            image_name, tag = input_name.split(":")
        else:
            image_name = input_name
            tag = None

        name_search = self.conn.images(name=image_name, all=True)
        # We found only one result, return it
        if len(name_search) == 1:
            return name_search[0]['Id']

        else:
            # We found multiple images with the input name
            # If a tag is passed, then we can return the right one
            # If not, we assume if all the image_ids are same, we
            # can use that.

            ilist = []
            for image in name_search:
                if input_name in image['RepoTags']:
                    return image['Id']
                else:
                    ilist.append(image['Id'])
            if tag is not None:
                raise DockerMountError("Unable to find to an image named {0}"
                                       .format(input_name))
            # We didn't find it by name only. We check if the image_ids
            # are all the same
            if len(ilist) > 1:
                if all(ilist[0] == image for image in ilist) and (tag is None):
                    return ilist[0]
                else:
                    raise DockerMountError("Found multiple images named {0} "
                                           "with different image Ids.  Try "
                                           "again with the image name and tag"
                                           .format(input_name))
        return None

    def get_iid(self, input_name):
        '''
        Find the image id based on a input_name which can be
        an image id, image name, or an image name:tag name.
        '''

        # Check if the input name is a container
        cid = self.get_cid(input_name)

        if cid is not None:
            return cid, "container"

        dtype = "image"

        # Check if the input_name was an image name or name:tag
        image_id = self._namesearch(input_name)
        if image_id is not None:
            return image_id, dtype

        # Maybe input name is an image id (or portion)
        all_images = self.conn.images(all=True)
        for image in all_images:
            if image['Id'].startswith(input_name):
                return image['Id'], dtype

        raise DockerMountError("Unable to associate {0} with any image"
                               .format(input_name))

    def create_container(self, iid):
        '''
        Simple creation of a container
        '''
        container = self.conn.create_container(iid, command="/bin/true")
        return container['Id']

    def remove_container(self, cid):
        '''
        Simple removal of a container
        '''
        return self.conn.remove_container(cid)

    def activate_thin_device(self, image_tuple):
        '''
        Activates a thin device when given an image tuple
        containing the thin device size and thin device id.

        It returns a tuple that contains the return code,
        stderrm and stdout output.
        '''
        pool_name = self.docker_info['DriverStatus'][0][1]
        if (self.is_device_active(self.dev_name)).return_code == 1:
            table = '0 {0} thin /dev/mapper/{1} {2}'\
                    ''.format(str(image_tuple.thin_device_size), pool_name,
                              str(image_tuple.thin_device_id))

            cmd = ['dmsetup', '--noudevsync', 'create', self.dev_name,
                   '--table', '{0}'.format(table)]
            return self.subp(cmd)
        else:
            return self.return_tuple(1,
                                     stderr='Device {0} is already '
                                            'active'.format(self.dev_name),
                                     stdout='')

    def remove_thin_device(self, device):
        '''
        Removes the thin device given a device name
        '''
        cmd = ['dmsetup', '--noudevsync', 'remove', device]
        return self.subp(cmd)

    def wait_udev_settle(self, wait_device=None):
        '''
        Watches the udev event queue, and exits if all current events are
        handled
        '''
        cmd = ['udevadm', 'settle']
        if wait_device:
            cmd.extend(('--exit-if-exists', wait_device))
        return self.subp(cmd)

    def subp(self, cmd):
        '''
        Standard subprocess definition that controls return
        values in a tuple
        '''
        dm_proc = subprocess.Popen(cmd, stderr=subprocess.PIPE,
                                   stdout=subprocess.PIPE)
        out, err = dm_proc.communicate()

        return self.return_tuple(return_code=dm_proc.returncode,
                                 stderr=err, stdout=out)

    def _get_fs(self, thin_pathname):
        '''
        Returns the file system type (xfs, ext4) of a given
        device
        '''
        cmd = ['lsblk', '-o', 'FSTYPE', '-n', thin_pathname]
        fs_return = self.subp(cmd)
        return fs_return.stdout.strip()

    # image-input can be an image id or image name
    def mount(self, image_input):
        '''
        When given an image id, mount will create a temporary
        container based on the image, create a thin pool device,
        create a temporary dir at the specific mount point, and
        mount the thin pool device to the temporary mount point.

        If any of the above steps fail, a DockerMountError is raised.
        The mount functions returns a tuple with the container_id,
        thin_pathname, mount_path, and thin_dev_name.

        This tuple can be passed to the cleanup def to remove all the
        temporary information.
        '''
        try:
            iid, dtype = self.get_iid(image_input)
            if dtype is "image":
                container_id = self.create_container(iid)
            else:
                container_id = iid

            image_tuple = self.get_image_info(iid, dtype)
            self.dev_name = "thin-{0}-{1}".format(image_tuple.iid,
                                                  container_id[:6])
            activate = self.activate_thin_device(image_tuple)
            mount_point = "/mnt/" if self.mnt_point is None else self.mnt_point
            mnt_dir = os.path.join(mount_point, self.dev_name)
            thin_pathname = os.path.join("/dev/mapper", self.dev_name)
            self.wait_udev_settle(wait_device=thin_pathname)
            return_info = self._mount(container_id=container_id,
                                      thin_pathname=thin_pathname,
                                      mount_path=mnt_dir,
                                      thin_dev_name=self.dev_name,
                                      dtype=dtype)
            if activate.return_code == 1:
                # Activating the thin pool failed
                # Clean up container
                self.remove_container(container_id)
                raise DockerMountError("Activating thin pool failed "
                                       "with: {0}".format(activate.stderr))

            if os.path.exists(mnt_dir) and self.override is False:
                # The directory already exists
                # Clean up container
                # Clean up the thin pool
                self.remove_thin_device(self.dev_name)
                self.remove_container(container_id)
                raise DockerMountError("The directory {0} already exists. "
                                       "Choose a different mount point or pass"
                                       " the --override to the DockerMount "
                                       "class.".format(mnt_dir))
            elif not self.override:
                os.mkdir(mnt_dir)

            mount_options = "ro"
            fstype = str(self._get_fs(return_info.thin_pathname))
            if fstype.upper() == "XFS":
                mount_options = mount_options + ",nouuid"
            cmd = ['mount', '-o', '{0}'.format(mount_options),
                   return_info.thin_pathname, return_info.mount_path]
            make_mount = self.subp(cmd)
            if make_mount.return_code is not 0:
                raise DockerMountError("Unable to mount the thin-pool: "
                                       "{0}".format(make_mount.stderr))

        except DockerMountError as dm_err:
            return dm_err

        return return_info

    def unmount(self, path_name):
        '''
        Allows for a standalone umount and clean up of things when
        only the mount-point is known.  This is useful for those
        using this class in standalone mode where the image_tuple
        returned from mount is lost
        '''

        cmd = ['findmnt', '-n', '-o', 'SOURCE', path_name]
        out = self.subp(cmd)
        device_name = (out.stdout).strip()
        thin_dev_name = os.path.basename(device_name)
        iid, short_cid = thin_dev_name.replace("thin-", "").split("-")
        # FIXME
        # I dont love that the iid is defined here again
        iid, dtype = (self.get_iid(iid))
        cid = self.get_cid(short_cid)
        umount_tuple = self._mount(container_id=cid,
                                   thin_pathname=device_name,
                                   mount_path=path_name,
                                   thin_dev_name=os.path.basename(device_name),
                                   dtype=dtype)
        self.cleanup(umount_tuple)

        # FIXME
        # Do we need or want a return of soome sort?

    def cleanup(self, return_info):
        '''
        When given the tuple returned from mount, cleanup will
        umount the temporary mount, remove the thin device,
        remove the temporary container, and remove the temporary
        directory.
        '''
        try:
            # umount the mnt_dir
            cmd = ['umount', return_info.mount_path]
            mount_return_code = 1
            umount_counter = 1
            umount_max = 10
            while (mount_return_code is not 0):
                make_unmount = self.subp(cmd)
                mount_return_code = make_unmount.return_code
                if mount_return_code is not 0:
                    logging.debug("Unable to unmount {0} on attempt {1} of "
                                  "{2} due to {3}"
                                  .format(return_info.mount_path,
                                          umount_counter,
                                          umount_max,
                                          make_unmount.stderr))

                time.sleep(1)
                if umount_counter == umount_max:
                    raise DockerMountError("Unable to mount {0} due to {1}: "
                                           .format(return_info.mount_path,
                                                   make_unmount.stderr))
                    break

            # Deprecated for above, but keeping temporarily
            # FIXME; delete when ready

            # if make_unmount.return_code is not 0:
            #     raise DockerMountError("Unable to mount {0} due to {1}: "
            #                            .format(return_info.mount_path,
            #                                    make_unmount.stderr))

            remove_thin = self.remove_thin_device(return_info.thin_dev_name)
            if remove_thin.return_code is not 0:
                raise DockerMountError("Unable to remove thin device {0} "
                                       "due to {1}"
                                       .format(return_info.thin_dev_name,
                                               remove_thin.stderr))

            # remove the thin device
            try:
                os.rmdir(return_info.mount_path)

            except OSError as os_err:
                raise DockerMountError("Unable to remove dir {0} due to {1}"
                                       .format(return_info.mount_path, os_err))

            # Remove the temporary container now
            if return_info.dtype is "image":
                self.remove_container(return_info.container_id)

            # rm the mnt_dir
        except DockerMountError as dm_err:
            return dm_err
