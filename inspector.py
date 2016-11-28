#!/usr/bin/env python
#
# Rocket.Chat.Audit - inspector.py
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
Rocket.Chat.Auditor Inspector.

Usage:
  inspector.py [-v | -vv] [--host=<rocketchat_host>] [--time=<timestring>] logs
  inspector.py [-v | -vv] [--host=<rocketchat_host>] [--time=<timestring>] files
  inspector.py [-v | -vv] [--host=<rocketchat_host>] [--time=<timestring>] [--from=<addr>] [--dry-run] email <address>
  inspector.py (-h | --help)
  inspector.py --version

Positional Arguments:
  address           Send audit logs to this email address.

Options:
  -H --host=<host>  Rocket.Chat hostname or MongoDB URI. [default: localhost]
  -t --time=<time>  String like "today" or "-24h" or "2016-10-11,2016-10-13" [default: today].
  -f --from=<addr>  Address from which to send email [default: rocketchat@localhost].
  -d --dry-run      Run the operation in dry-run mode (e.g., print email rather than sending it)
  -v --verbose      Show verbose output during execution.
  -h --help         Show this screen.
  -V --version      Show the version.
"""

from bson import json_util
from datetime import datetime
from datetime import timedelta
from docopt import docopt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from gridfs import GridFS
from itertools import imap, groupby
import json
import logging
import pymongo
import pytz
import re
import smtplib
import sys


class Inspector(object):
    """
    Your friendly neighborhood Rocket.Chat.Audit inspector.

    You can ask it to show you the audit for a given time period, e.g.,
    a list of all chat logs or journal of file uploads.
    """

    LOOKBACK_RE = re.compile("(?P<value>-\d+)(?P<unit>d|h|m|s)")
    LOOKBACK_TRANSFORMS = {"s": 1, "m": 60, "h": 60*60, "d": 60*60*24}

    def __init__(self, messages, files):
        self.messages = messages
        self.files = files

    def list_files(self, timestring):
        return imap(lambda f: f.filename,
                    self.files.find({"uploadDate": self._timestring_to_query(timestring)}))

    def list_logs(self, timestring):
        return self.messages.find({"ts": self._timestring_to_query(timestring)})

    def _timestring_to_query(self, timestring):
        now = datetime.utcnow()
        # check special strings
        if timestring == "today":
            return {"$gte": self._midnight(now)}
        if timestring == "yesterday":
            return {"$gte": self._midnight(now + timedelta(days=-1)), "$lt": self._midnight(now)}
        # check lookback time (e.g., -180s, -3m, -2.5h, -0.5d)
        m = self.LOOKBACK_RE.match(timestring)
        if m:
            lookback = float(m.group('value')) * self.LOOKBACK_TRANSFORMS[m.group('unit')]
            return {"$gte": now + timedelta(seconds=lookback)}
        raise Exception("unknown timestring format: %s" % timestring)

    @staticmethod
    def _midnight(d):
        return d.replace(hour=0, minute=0, second=0)


class Archiver(object):
    """
    Prepares the daily Rocket.Chat.Audit archive.
    """

    def __init__(self, inspector):
        self.inspector = inspector
        self.logger = logging.getLogger(self.__class__.__name__)

    def send_email(self,
                   timestring,
                   from_email,
                   to_email,
                   subject="Rocket.Chat.Archive",
                   preamble="Archives for %s attached",
                   dry_run=False):
        email = self._prepare_email(timestring, from_email, to_email, subject, preamble)
        self.logger.info("Sending email to %s\n%s" % (to_email, self._indent(email.as_string())))
        if not dry_run:
            self._send_email(email, from_email, to_email)

    # PUBLIC HELPERS

    @staticmethod
    def print_msg(doc):
        ts = pytz.utc.localize(doc['ts']).astimezone(pytz.timezone("US/Central"))
        return "%s %s: %s" % (ts.isoformat(), doc['username'], doc['msg'])

    @staticmethod
    def group_by(data, key_func):
        groups = {}
        data = sorted(data, key=key_func)
        for k, g in groupby(data, key_func):
            groups[k] = list(g)
        return groups

    # PRIVATE

    def _prepare_email(self,
                       timestring,
                       from_email,
                       to_email,
                       subject="Rocket.Chat.Archive",
                       preamble="Archives for %s attached",):
        email = MIMEMultipart()
        email['Subject'] = subject
        email['From'] = from_email
        email['To'] = to_email
        email.attach(MIMEText(preamble % timestring if "%s" in preamble else preamble))
        self._attach_chat_logs(email, timestring)
        self._attach_file_journal(email, timestring)
        return email

    def _attach_chat_logs(self, email, timestring):
        logs = self.group_by(self.inspector.list_logs(timestring), lambda e: e['room_name'])
        for room_name, room_log in logs.iteritems():
            # stop delaying the inevitable: read all the logs into memory for the email
            attachment = MIMEText("\n".join(imap(self.print_msg, room_log)))
            attachment.add_header('Content-Disposition', 'attachment',
                                  filename="%s.txt" % room_name)
            email.attach(attachment)

    def _attach_file_journal(self, email, timestring):
        files = self.inspector.list_files(timestring)
        files_attachment = MIMEText("\n".join(files))
        files_attachment.add_header('Content-Disposition', 'attachment', filename="files.txt")
        email.attach(files_attachment)

    # PRIVATE HELPERS

    @staticmethod
    def _send_email(msg, from_addr, to_addr, smtp_host='localhost'):
        s = smtplib.SMTP(smtp_host)
        s.sendmail(from_addr, [to_addr], msg.as_string())
        s.quit()

    @staticmethod
    def _indent(text, prefix='\t'):
        # unfortunately textwrap#indent only added in python 3.3
        # https://github.com/python/cpython/blob/master/Lib/textwrap.py#L467
        return ''.join([prefix + line for line in text.splitlines(True)])


def to_json(l):
    return json.dumps(list(l), indent=2, default=json_util.default)


def main(rocketchat_host, timestring, arguments):
    client = pymongo.MongoClient(rocketchat_host)
    grid = GridFS(client['rocketchat_audit'], collection='file_uploads')
    inspector = Inspector(client['rocketchat_audit']['messages'], grid)

    if arguments['files']:
        def print_files(doc):
            return doc
        print to_json(imap(print_files, inspector.list_files(timestring)))
    elif arguments['logs']:
        logs = Archiver.group_by(inspector.list_logs(timestring), lambda e: e['room_name'])
        print json.dumps({k: map(Archiver.print_msg, v) for k, v in logs.iteritems()}, indent=2)
    elif arguments['email']:
        archiver = Archiver(inspector)
        archiver.send_email(timestring, arguments['--from'], arguments['<address>'],
                            dry_run=arguments['--dry-run'])


if __name__ == '__main__':
    arguments = docopt(__doc__, version='Rocket.Chat.Audit Inspector 1.0')
    level = [logging.WARNING, logging.INFO, logging.DEBUG][arguments['--verbose']]
    log_format = '%(asctime)s %(levelname)s: %(message)s'
    logging.basicConfig(level=level, format=log_format, stream=sys.stderr)
    main(arguments['--host'], arguments['--time'], arguments)
