#!/usr/bin/env python
#
# Rocket.Chat.Audit - rocketchat.audit.py
#
# Copyright 2016 Peak6 Investments, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Audits Rocket.Chat communications for compliance.

Tails the MongoDB oplog for efficient real-time auditing.
"""


from abc import abstractmethod
from abc import ABCMeta
import argparse
import logging
import os.path
import shutil
import sys
import time
import traceback

from filecachetools import LRUCache
import pymongo


class Auditor(object):
    """
    Rocket.Chat auditor which tails the MongoDB oplog.

    Passes each event to the appropriate AuditHandler callback.
    """

    def __init__(self, handler, cache_size=10000):
        self.handler = handler
        self.logger = logging.getLogger(self.__class__.__name__)
        # this cache exists because only changed data is replicated in the oplog
        # the room ID is never changed, so we have to map to the message ID
        # the editedBy username isn't changed if you edit multiple times, so we need that too
        # format: message _id => (room rid, editedBy username)
        self.cache = LRUCache(name='rocketchat_audit_messages', maxsize=cache_size)

    def tail_latest(self, oplog):
        first = oplog.find().sort('$natural', pymongo.DESCENDING).limit(-10).next()
        self.tail(oplog, first['ts'])

    def tail(self, oplog, ts):
        cursor = oplog.find({'ts': {'$gt': ts}}, oplog_replay=True,
                            cursor_type=pymongo.cursor.CursorType.TAILABLE_AWAIT)
        while cursor.alive:
            for doc in cursor:
                self._parse(doc)

    def _parse(self, doc):
        o = doc['o']
        ns = doc['ns']
        self.logger.debug("DOC %s", doc)
        # insert a new message
        if doc['op'] == 'i' and ns.endswith("rocketchat_message") and o.get('msg', False):
            self.logger.info("INSERT %s", doc)
            self.cache[o['_id']] = (o['rid'], None)  # so edits know the room for each message
            self.handler.on_message(o['rid'], str(o['ts']), o['u']['username'], o['msg'])
        # update an existing message
        elif doc['op'] == 'u' and ns.endswith("rocketchat_message"):
            self.logger.info("UPDATE %s", doc)
            s = o['$set']
            msg_id = doc['o2']['_id']
            # cache persists across restarts but messages eventually evicted, so we need a default
            rid, edited_by = self.cache.get(msg_id, ('#unknown', 'unknown.user'))
            edited_by = s['editedBy']['username'] if 'editedBy' in s else edited_by
            self.cache[msg_id] = (rid, edited_by)  # so edits know the room for each message
            self.handler.on_message(rid, str(s['editedAt']), edited_by, s['msg'])
        # upload a file attachment
        elif ns.endswith("rocketchat_message") and "attachments" in o:
            self.logger.info("FILE %s", doc)
            title = o['attachments'][0]['title']
            self.handler.on_file(o['rid'], str(o['ts']), o['u']['username'], title,
                                 o['file']['_id'], o['attachments'][0]['image_type'])


class AuditHandler(object):
    """
    Base class for AuditHandlers which define callbacks for different Rocket.Chat events.
    """
    __metaclass__ = ABCMeta

    @abstractmethod
    def on_message(self, room_id, ts, username, msg):
        pass

    @abstractmethod
    def on_file(self, room_id, ts, username, title, file_id, image_type):
        pass


class FileAuditHandler(AuditHandler):
    """
    AuditHandler which writes one file per chat room (channel, private group, or direct message),
    journals all file uploads to a special file, and copies all files to an archive directory.
    """

    def __init__(self, audit_dir, file_store, file_archive, file_journal='file_uploads'):
        """
        :param audit_dir: directory in which to store per-room audit logs
        :param file_store: Rocket.Chat File Upload file system path configuration
        :param file_archive: directory to which File Uploads should be copied for archiving
        :param file_journal: file to which to record File Uploads
        """
        self.audit_dir = audit_dir
        self.file_store = file_store
        self.file_archive = file_archive
        self.file_journal = file_journal

    def on_message(self, room_id, ts, username, msg):
        self._log(room_id, ts, username, msg)

    def on_file(self, room_id, ts, username, title, file_id, image_type):
        ext = image_type.split("/")[1]
        filename = "%s.%s" % (file_id, ext)
        message = "%s %s" % (title, filename)
        # log file uploads both to the room and the file_journal
        self._log(room_id, ts, username, message)
        self._log(self.file_journal, ts, username, message)
        # copy the file from Rocket.Chat's file store to the file archive
        shutil.copy(self.file_store + filename, self.file_archive + filename)

    def _log(self, filename, ts, username, msg):
        with open("%s%s" % (self.audit_dir, filename), 'a') as target:
            target.write("%s %s: %s\n" % (ts, username, msg))


def main(audit_dir, file_store, file_archive, is_master_slave, host):
    print {"audit_dir": audit_dir, "file_store": file_store, "file_archive": file_archive}
    c = pymongo.MongoClient(host)

    auditor = Auditor(FileAuditHandler(audit_dir, file_store, audit_dir))
    oplog = c.local.oplog['$main'] if is_master_slave else c.local.oplog.rs

    while True:
        try:
            auditor.tail_latest(oplog)
            time.sleep(1)
        except Exception, e:
            traceback.print_exc(e)


# MUST match your Rocket.Chat configuration for File Upload file system path
ROCKETCHAT_FILE_UPLOAD_DIRECTORY = "/var/lib/rocketchat.filestore/"

AUDIT_FILE_UPLOAD_DIRECTORY = "/var/lib/rocketchat.audit/filearchive/"
AUDIT_CHAT_DIRECTORY = "/var/lib/rocketchat.audit/chats/"

if __name__ == '__main__':
    norm = lambda d: os.path.join(d, '')  # normalize directory so that it ends with a slash
    parser = argparse.ArgumentParser(description='Audit Rocket.Chat Communications')
    parser.add_argument('-o', '--output', dest='audit_dir', type=norm, default=AUDIT_CHAT_DIRECTORY,
                        help='Directory in which per-room audit logs should be stored')
    parser.add_argument('-f', '--file-store', type=norm, default=ROCKETCHAT_FILE_UPLOAD_DIRECTORY,
                        help='Rocket.Chat File Upload file system path configuration')
    parser.add_argument('-a', '--file-archive', type=norm, default=AUDIT_FILE_UPLOAD_DIRECTORY,
                        help='Directory where File Uploads should be copied for archiving')
    parser.add_argument('-m', '--master-slave', action='store_true',
                        help='True if using master-slave oplog instead of replica set')
    parser.add_argument('-H', '--host', help='MongoDB hostname or URI')
    parser.add_argument('-v', '--verbose', action='count', help='verbose output')
    args = parser.parse_args()

    log_format = '%(asctime)s %(levelname)s: %(message)s'
    level = [logging.WARNING, logging.INFO, logging.DEBUG][args.verbose or 0]
    logging.basicConfig(level=level, format=log_format, stream=sys.stderr)
    main(args.audit_dir, args.file_store, args.file_archive, args.master_slave, args.host)
