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
from datetime import datetime
import logging
import operator
import sys
import time
import traceback

from filecachetools import LRUCache, cachedmethod
from gridfs import GridFS
import pymongo


class Auditor(object):
    """
    Rocket.Chat auditor which tails the MongoDB oplog.

    Passes each event to the appropriate AuditHandler callback.
    """

    def __init__(self, rocketchat, handler, message_cache_size=10000):
        self.rocketchat = rocketchat
        self.handler = handler
        self.logger = logging.getLogger(self.__class__.__name__)
        # this cache exists for edits -- only changed data is replicated in the oplog
        # the room ID is never changed, so we have to map to the message ID
        # the editedBy username isn't changed if you edit multiple times, so we need that too
        # format: message _id => (room rid, editedBy username)
        self.message_cache = LRUCache(name='rocketchat_audit_messages', maxsize=message_cache_size)

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
            room_name = self.rocketchat.get_room_name(o['rid'])
            self.handler.on_message(o['rid'], room_name, str(o['ts']), o['u']['username'], o['msg'])
        # update an existing message
        elif doc['op'] == 'u' and ns.endswith("rocketchat_message"):
            self.logger.info("UPDATE %s", doc)
            s = o['$set']
            msg_id = doc['o2']['_id']
            rid, room_name, edited_by = self.rocketchat.get_message_room_and_editor(msg_id)
            edited_by = s['editedBy']['username'] if 'editedBy' in s else edited_by
            self.message_cache[msg_id] = (rid, edited_by)  # so edits know the room for each msg
            room_name = self.rocketchat.get_room_name(o['rid'])
            self.handler.on_message(rid, room_name, str(s['editedAt']), edited_by, s['msg'])
        # upload a file attachment
        elif ns.endswith("rocketchat_message") and "attachments" in o:
            self.logger.info("FILE %s", doc)
            title = o['attachments'][0]['title']
            room_name = self.rocketchat.get_room_name(o['rid'])
            self.handler.on_file(o['rid'], room_name, str(o['ts']), o['u']['username'], title,
                                 o['file']['_id'], o['attachments'][0]['image_type'])


class RocketChat(object):
    """
    Queries RocketChat via direct access to the database using a read-through cache pattern.
    """

    def __init__(self, rocketchat_db, room_cache_size=10000, message_cache_size=10000):
        self.rooms = rocketchat_db['rocketchat_room']
        self.messages = rocketchat_db['message']
        self.room_cache = LRUCache(name='rocketchat_audit_rooms', maxsize=room_cache_size)
        self.message_cache = LRUCache(name='rocketchat_audit_messages', maxsize=message_cache_size)

    @cachedmethod(operator.attrgetter('room_cache'))
    def get_room_name(self, room_id):
        room = self.rooms.find_one({"_id": room_id})
        # channels and private groups (t: c and t: p)
        if "name" in room:
            return room['name']
        # direct messages
        if room['t'] == "d":
            return "_x_".join(room['usernames'])

    @cachedmethod(operator.attrgetter('message_cache'))
    def get_message_room_and_editor(self, message_id):
        """
        # cache persists across restarts and reads-through to DB if evicted
        """
        # message = self.messages.find_one({"_id": message_id})
        # room_name = self.get_room_name(message['room_id'])
        # return {"room_name": room_name, "editedBy": message['editedBy']}
        pass


class AuditHandler(object):
    """
    Base class for AuditHandlers which define callbacks for different Rocket.Chat events.
    """
    __metaclass__ = ABCMeta

    @abstractmethod
    def on_message(self, room_id, room_name, ts, username, msg):
        pass

    @abstractmethod
    def on_file(self, room_id, room_name, ts, username, title, file_id, image_type):
        pass


class MongoLoggingAuditHandler(AuditHandler):
    """
    AuditHandler which logs all messages to an audit mongodb database.

    All messages are logged across all chat rooms (channel, private group, or direct message).
    Files are also copied to the audit db.

    There are two collections:
    - messages: contains a document for all messages (inserts and updates) across all rooms
    - files: contains the gridfs representations of all File Uploads
    """

    def __init__(self, rocketchat_db, audit_db):
        """
        :param rocketchat_db: the mongodb audit log
        :param audit_db: the mongodb database used for writing audit logs
        """
        self.rocketchat_gridfs = GridFS(rocketchat_db, collection='rocketchat_uploads')
        self.audit_gridfs = GridFS(audit_db, collection='file_uploads')
        self.messages = audit_db['messages']

    def on_message(self, room_id, room_name, ts, username, msg):
        self._log(room_id, room_name, ts, username, msg)

    def on_file(self, room_id, room_name, ts, username, title, file_id, image_type):
        message = "%s [%s %s]" % (title, file_id, image_type)
        self._log(room_id, room_name, ts, username, message)
        self._archive_file_uploads(file_id, title)

    def _archive_file_uploads(self, file_id, title):
        uploaded_file = self.rocketchat_gridfs.get(file_id)
        self.audit_gridfs.put(uploaded_file, filename=title,
                              content_type=uploaded_file.content_type)

    def _log(self, room_id, room_name, ts, username, msg):
        ts = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
        self.messages.insert({"room_id": room_id, "room_name": room_name,
                              "ts": ts, "username": username, "msg": msg})


def main(host):
    client = pymongo.MongoClient(host)
    rocketchat = RocketChat(client['rocketchat'])
    handler = MongoLoggingAuditHandler(client['rocketchat'], client['rocketchat_audit'])
    auditor = Auditor(rocketchat, handler)
    oplog = client.local.oplog.rs

    while True:
        try:
            auditor.tail_latest(oplog)
            time.sleep(1)
        except Exception, e:
            traceback.print_exc(e)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Audit Rocket.Chat Communications')
    parser.add_argument('-H', '--host', help='MongoDB hostname or URI; defaults to localhost')
    parser.add_argument('-v', '--verbose', action='count', help='verbose output')
    args = parser.parse_args()

    log_format = '%(asctime)s %(levelname)s: %(message)s'
    level = [logging.WARNING, logging.INFO, logging.DEBUG][args.verbose or 0]
    logging.basicConfig(level=level, format=log_format, stream=sys.stderr)
    main(args.host)
