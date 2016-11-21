# Rocket.Chat.Audit

Rocket.Chat.Audit is an application and library to inspect and record all
Rocket.Chat communications as required by your Compliance Department.

Out of the box, this application writes all audit files locally. It is expected
that a batch process will regularly import them into downstream auditing systems.

If you need real-time journaling or have other audit requirements, it should be
quite simple to fork and build on this framework. Patches are welcome. :)

## Configuration

You can control how the oplog is tailed with the following common parameters:

| Name            | Flag | Option           | Description                                             |
| --------------- | ---- | ---------------- | ------------------------------------------------------- |
| Master/Slave?   | `-m` | `--master-slave` | True if using master-slave oplog instead of replica set |
| Host Name       | `-H` | `--host`         | MongoDB hostname or URI; defaults to localhost          |


There are three parameters for file-based auditing:

| Name            | Flag | Option           | Description                                             |
| --------------- | ---- | ---------------- | ------------------------------------------------------- |
| Audit Directory | `-o` | `--output`       | Directory in which per-room audit logs should be stored |
| File Store      | `-f` | `--file-store`   | "gridfs" or Rocket.Chat File Upload file system path    |
| File Archive    | `-a` | `--file-archive` | Directory where File Uploads should be copied for archiving |

## Installation

An [ansible deployer](./deploy/) is in included for Ubuntu 14.04.
The steps will be similar for other distributions.

### Pre Installation

1. Convert your Rocket.Chat MongoDB from a standalone [to a replica set]
(https://docs.mongodb.org/manual/tutorial/convert-standalone-to-replica-set/).

2. Configure your Rocket.Chat instance to store File Uploads on the File System.

3. Disable "Keep Message History" in Rocket.Chat to avoid edited messages 
   appearing in your audit logs multiple times.

### Configuration

Prior to install, its best to figure out a few things. The installation itself is opinionated
out of the box but can configured however you want. The can all be configured in
[`deploy/config.yml`](./deploy/config.yml)

1. What user does Rocket.Chat run as? (`user` and `group`)

2. Where does Rocket.Chat store File Uploads? (`file_store`)

3. Where do you want the audit logs stored? (`audit_dir`)

4. Where do you want the audit file archive stored? This could take a lot of space! (`file_archive`)

### Deployment

1. Add your host names in `inventory` for your different environments.

2. Update the `remote_user` in `config.yaml` to SSH as this user.

3. Make sure you have a public key on each host in your inventory for `remote_user`.

4. Customize `config.yaml` for your installation.

5. To deploy to a specific environment `env`, run

   ansible-playbook -i inventory deploy.yaml -e env=development

## Running

Ansible installs Rocket.Chat.Audit as a service named `rocketchat.audit`.
You can start, stop, or check that status with

    sudo service rocketchat.audit start
    sudo service rocketchat.audit stop
    sudo service rocketchat.audit status

## LICENSE

Rocket.Chat.Audit is available under the Apache License, Version 2.0.

    Copyright 2016 Peak6 Investments, L.P.

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
