#!/usr/bin/python
#
# Copyright 2017 Google Inc.
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

# Start-up script for ESP.
# Configures nginx and fetches service configuration.
#
# Exit codes:
#     1 - failed to fetch,
#     2 - validation error,
#     3 - IO error,
#     4 - argument parsing error,
#     in addition to NGINX error codes.

import argparse
import collections
import fetch_service_config as fetch
import json
import logging
import os
import re
import sys
import textwrap
import uuid

from collections import Counter
from mako.template import Template

# Location of NGINX binary
NGINX = "/usr/sbin/nginx"

# Location of NGINX template
NGINX_CONF_TEMPLATE = "/etc/nginx/nginx-auto.conf.template"
SERVER_CONF_TEMPLATE = "/etc/nginx/server-auto.conf.template"

# Location of generated config files
CONFIG_DIR = "/etc/nginx/endpoints"

# Protocol prefixes
GRPC_PREFIX = "grpc://"
HTTP_PREFIX = "http://"
HTTPS_PREFIX = "https://"

# Metadata service
METADATA_ADDRESS = "http://169.254.169.254"

# Service management service
SERVICE_MGMT_URL_TEMPLATE = (
    "https://servicemanagement.googleapis.com"
    "/v1/services/{}/config?configId={}")

# DNS resolver
DNS_RESOLVER = "8.8.8.8"

# Default HTTP/1.x port
DEFAULT_PORT = 8080

# Default status port
DEFAULT_STATUS_PORT = 8090

# Default backend
DEFAULT_BACKEND = "127.0.0.1:8081"

# Default rollout_strategy
DEFAULT_ROLLOUT_STRATEGY = "fixed"

# PID file (for nginx as a daemon)
PID_FILE = "/var/run/nginx.pid"

# Google default application credentials environment variable
GOOGLE_CREDS_KEY = "GOOGLE_APPLICATION_CREDENTIALS"

Port = collections.namedtuple('Port',
        ['port', 'proto'])
Location = collections.namedtuple('Location',
        ['path', 'backends', 'proto'])
Ingress = collections.namedtuple('Ingress',
        ['ports', 'host', 'locations'])

def write_pid_file():
    try:
        f = open(PID_FILE, 'w+')
        f.write(str(os.getpid()))
        f.close()
    except IOError as err:
        logging.error("Failed to save PID file: " + PID_FILE)
        logging.error(err.strerror)
        sys.exit(3)

def write_template(ingress, nginx_conf, args):
    # Load template
    try:
        template = Template(filename=args.template)
    except IOError as err:
        logging.error("Failed to load NGINX config template. " + err.strerror)
        sys.exit(3)

    conf = template.render(
            ingress=ingress,
            pid_file=PID_FILE,
            server_config=args.server_config,
            status=args.status_port,
            service_account=args.service_account_key,
            metadata=args.metadata,
            resolver=args.dns,
            access_log=args.access_log,
            healthz=args.healthz,
            tls_mutual_auth=args.tls_mutual_auth)

    # Save nginx conf
    try:
        f = open(nginx_conf, 'w+')
        f.write(conf)
        f.close()
    except IOError as err:
        logging.error("Failed to save NGINX config." + err.strerror)
        sys.exit(3)

def write_server_config_templage(server_config, args):
    # Load template
    try:
        template = Template(filename=args.server_config_template)
    except IOError as err:
        logging.error("Failed to load server config template. " + err.strerror)
        sys.exit(3)

    conf = template.render(
             service_configs=args.service_configs,
             rollout_id=args.rollout_id,
             rollout_strategy=args.rollout_strategy)

    # Save nginx conf
    try:
        f = open(server_config, 'w+')
        f.write(conf)
        f.close()
    except IOError as err:
        logging.error("Failed to save server config." + err.strerror)
        sys.exit(3)


def ensure(config_dir):
    if not os.path.exists(config_dir):
        try:
            os.makedirs(config_dir)
        except OSError as exc:
            logging.error("Cannot create config directory.")
            sys.exit(3)


def assert_file_exists(fl):
    if not os.path.exists(fl):
        logging.error("Cannot find the specified file " + fl)
        sys.exit(3)


def start_nginx(nginx, nginx_conf):
    try:
        # Control is relinquished to nginx process after this line
        os.execv(nginx, ['nginx', '-p', '/usr', '-c', nginx_conf])
    except OSError as err:
        logging.error("Failed to launch NGINX: " + nginx)
        logging.error(err.strerror)
        sys.exit(3)


def fetch_and_save_service_config(args, token, version, filename):
    try:
        # build request url
        service_mgmt_url = SERVICE_MGMT_URL_TEMPLATE.format(args.service,
                                                    version)
        # Validate service config if we have service name and version
        logging.info("Fetching the service configuration "\
                     "from the service management service")
        # download service config
        config = fetch.fetch_service_json(service_mgmt_url, token)

        # Save service json for ESP
        service_config = args.config_dir + "/" + filename

        try:
            f = open(service_config, 'w+')
            json.dump(config, f, sort_keys=True, indent=2,
                      separators=(',', ': '))
            f.close()
        except IOError as err:
            logging.error("Cannot save service config." + err.strerror)
            sys.exit(3)

    except fetch.FetchError as err:
        logging.error(err.message)
        sys.exit(err.code)

# config_id might have invalid character for file name.
def generate_service_config_filename(version):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, str(version)))

def fetch_service_config(args):
    args.service_configs = {};

    try:
        if args.service_config_url is not None:
            service_mgmt_url = args.service_config_url
        else:
            # fetch service name, if not specified
            if args.service is None:
                logging.info(
                    "Fetching the service name from the metadata service")
                args.service = fetch.fetch_service_name(args.metadata)

            # if service name is not specified, display error message and exit
            if args.service is None:
                logging.error("Unable to get service name");
                sys.exit(3)

            # fetch service config ID, if not specified
            if args.version is None:
                logging.info("Fetching the service config ID "\
                             "from the metadata service")
                args.version = fetch.fetch_service_config_id(args.metadata)

            # Get the access token
            if args.service_account_key is None:
                logging.info("Fetching an access token from the metadata service")
                token = fetch.fetch_access_token(args.metadata)
            else:
                token = fetch.make_access_token(args.service_account_key)

            # Fetch api version from latest successful rollouts
            if args.version is None:
                logging.info(
                    "Fetching the service config ID from the rollouts service")
                rollout = fetch.fetch_latest_rollout(args.service, token)
                args.rollout_id = rollout["rolloutId"]
                for version, percentage in rollout["trafficPercentStrategy"]["percentages"].iteritems():
                    filename = generate_service_config_filename(version)
                    fetch_and_save_service_config(args, token, version, filename)
                    args.service_configs[args.config_dir + "/" + filename] = percentage;
            else:
                filename = generate_service_config_filename(args.version)
                fetch_and_save_service_config(args, token, args.version, filename)
                args.service_configs[args.config_dir + "/" + filename] = 100;

    except fetch.FetchError as err:
        logging.error(err.message)
        sys.exit(err.code)


def make_ingress(args):
    ports = []

    # Set port by default
    if (args.http_port is None and
        args.http2_port is None and
        args.ssl_port is None):
        args.http_port = DEFAULT_PORT

    # Check for port collisions
    collisions = Counter([
            args.http_port, args.http2_port,
            args.ssl_port, args.status_port])
    collisions.pop(None, 0)
    if len(collisions) > 0:
        shared_port, count = collisions.most_common(1)[0]
        if count > 1:
            logging.error("Port " + str(shared_port) + " is used more than once.")
            sys.exit(2)

    if args.http_port is not None:
        ports.append(Port(args.http_port, "http"))
    if args.http2_port is not None:
        ports.append(Port(args.http2_port, "http2"))
    if args.ssl_port is not None:
        ports.append(Port(args.ssl_port, "ssl"))

    if args.backend.startswith(GRPC_PREFIX):
        proto = "grpc"
        backends = [args.backend[len(GRPC_PREFIX):]]
    elif args.backend.startswith(HTTP_PREFIX):
        proto = "http"
        backends = [args.backend[len(HTTP_PREFIX):]]
    elif args.backend.startswith(HTTPS_PREFIX):
        proto = "https"
        backend = args.backend[len(HTTPS_PREFIX):]
        if not re.search(r':[0-9]+$', backend):
            backend = backend + ':443'
        backends = [backend]
    else:
        proto = "http"
        backends = [args.backend]

    locations = [Location(
            path='/',
            backends=backends,
            proto=proto)]

    ingress = Ingress(
            ports=ports,
            host='""',
            locations=locations)

    return ingress

class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_help(sys.stderr)
        self.exit(4, '%s: error: %s\n' % (self.prog, message))

def make_argparser():
    parser = ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
            description = '''
ESP start-up script. This script fetches the service configuration from the
service management service and configures ESP to expose the specified ports and
proxy requests to the specified backend.

The service name and config ID are optional. If not supplied, the script
fetches the service name and the config ID from the metadata service as
attributes "{service_name}" and "{service_config_id}".

ESP relies on the metadata service to fetch access tokens for Google services.
If you deploy ESP outside of Google Cloud environment, you need to provide a
service account credentials file by setting {creds_key}
environment variable or by passing "-k" flag to this script.

If a custom nginx config file is provided ("-n" flag), the script launches ESP
with the provided config file. Otherwise, the script uses the exposed ports
("-p", "-P", "-S", "-N" flags) and the backend ("-a" flag) to generate an nginx
config file.'''.format(
        service_name = fetch._METADATA_SERVICE_NAME,
        service_config_id = fetch._METADATA_SERVICE_CONFIG_ID,
        creds_key = GOOGLE_CREDS_KEY
    ))

    parser.add_argument('-k', '--service_account_key', help=''' Use the service
    account key JSON file to access the service control and the service
    management.  You can also set {creds_key} environment
    variable to the location of the service account credentials JSON file. If
    the option is omitted, ESP contacts the metadata service to fetch an access
    token.  '''.format(creds_key = GOOGLE_CREDS_KEY))

    parser.add_argument('-s', '--service', help=''' Set the name of the
    Endpoints service.  If omitted and -c not specified, ESP contacts the
    metadata service to fetch the service name.  ''')

    parser.add_argument('-v', '--version', help=''' Set the service config ID of
    the Endpoints service.  If omitted and -c not specified, ESP contacts the
    metadata service to fetch the service config ID.  ''')

    parser.add_argument('-n', '--nginx_config', help=''' Use a custom nginx
    config file instead of the config template {template}. If you specify this
    option, then all the port options are ignored.
    '''.format(template=NGINX_CONF_TEMPLATE))

    parser.add_argument('-r', '--server_config', help=''' Use a server
    config file instead of the config template {template}. If you specify this
    option, then all the server options are ignored.
    '''.format(template=SERVER_CONF_TEMPLATE))

    parser.add_argument('-p', '--http_port', default=None, type=int, help='''
    Expose a port to accept HTTP/1.x connections.  By default, if you do not
    specify any of the port options (-p, -P, and -S), then port {port} is
    exposed as HTTP/1.x port. However, if you specify any of the port options,
    then only the ports you specified are exposed, which may or may not include
    HTTP/1.x port.  '''.format(port=DEFAULT_PORT))

    parser.add_argument('-P', '--http2_port', default=None, type=int, help='''
    Expose a port to accept HTTP/2 connections.  Note that this cannot be the
    same port as HTTP/1.x port.  ''')

    parser.add_argument('-S', '--ssl_port', default=None, type=int, help='''
    Expose a port for HTTPS requests.  Accepts both HTTP/1.x and HTTP/2
    secure connections. Requires the certificate and key files
    /etc/nginx/ssl/nginx.crt and /etc/nginx/ssl/nginx.key''')

    parser.add_argument('-N', '--status_port', default=DEFAULT_STATUS_PORT,
    type=int, help=''' Change the ESP status port. Status information is
    available at /endpoints_status location over HTTP/1.x. Default value:
    {port}.'''.format(port=DEFAULT_STATUS_PORT))

    parser.add_argument('-a', '--backend', default=DEFAULT_BACKEND, help='''
    Change the application server address to which ESP proxies the requests.
    Default value: {backend}. For HTTPS backends, please use "https://" prefix,
    e.g. https://127.0.0.1:8081. For HTTP/1.x backends, prefix "http://" is
    optional. For GRPC backends, please use "grpc://" prefix,
    e.g. grpc://127.0.0.1:8081.'''.format(backend=DEFAULT_BACKEND))

    parser.add_argument('-t', '--tls_mutual_auth', action='store_true', help='''
    Enable TLS mutual authentication for HTTPS backends.
    Default value: Not enabled. Please provide the certificate and key files
    /etc/nginx/ssl/backend.crt and /etc/nginx/ssl/backend.key.''')

    parser.add_argument('-c', '--service_config_url', default=None, help='''
    Use the specified URL to fetch the service configuration instead of using
    the default URL template
    {template}.'''.format(template=SERVICE_MGMT_URL_TEMPLATE))

    parser.add_argument('-z', '--healthz', default=None, help='''Define a
    health checking endpoint on the same ports as the application backend. For
    example, "-z healthz" makes ESP return code 200 for location "/healthz",
    instead of forwarding the request to the backend.  Default: not used.''')

    parser.add_argument('-R', '--rollout_strategy',
        default=DEFAULT_ROLLOUT_STRATEGY,
        help='''The service config rollout strategy, [fixed|managed],
        Default value: {strategy}'''.format(strategy=DEFAULT_ROLLOUT_STRATEGY),
        choices=['fixed', 'managed'])

    # Specify a custom service.json path.
    # If this is specified, service json will not be fetched.
    parser.add_argument('--service_json_path',
        default=None,
        help=argparse.SUPPRESS)

    # Customize metadata service url prefix.
    parser.add_argument('-m', '--metadata',
        default=METADATA_ADDRESS,
        help=argparse.SUPPRESS)

    # Fetched service config and generated nginx config are placed
    # into config_dir as service.json and nginx.conf files
    parser.add_argument('--config_dir',
        default=CONFIG_DIR,
        help=argparse.SUPPRESS)

    # nginx.conf template
    parser.add_argument('--template',
        default=NGINX_CONF_TEMPLATE,
        help=argparse.SUPPRESS)

    # nginx.conf template
    parser.add_argument('--server_config_template',
        default=SERVER_CONF_TEMPLATE,
        help=argparse.SUPPRESS)


    # nginx binary location
    parser.add_argument('--nginx',
        default=NGINX,
        help=argparse.SUPPRESS)

    # Address of the DNS resolver used by nginx http.cc
    parser.add_argument('--dns',
        default=DNS_RESOLVER,
        help=argparse.SUPPRESS)

    # Access log destination. Use special value 'off' to disable.
    parser.add_argument('--access_log',
        default='/dev/stdout',
        help=argparse.SUPPRESS)

    return parser


if __name__ == '__main__':
    parser = make_argparser()
    args = parser.parse_args()
    logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.INFO)

    # Set credentials file from the environment variable
    if args.service_account_key is None:
        if GOOGLE_CREDS_KEY in os.environ:
            args.service_account_key = os.environ[GOOGLE_CREDS_KEY]

    # Write pid file for the supervising process
    write_pid_file()

    # Get service config
    if args.service_json_path:
        assert_file_exists(args.service_json_path)
        args.service_configs = {args.service_json_path: 100}
    else:
        # Fetch service config and place it in the standard location
        ensure(args.config_dir)
        fetch_service_config(args)

    # Generate or check server_config
    if args.server_config:
        assert_file_exists(args.server_config)
    else:
        args.server_config = args.config_dir + "/server_config.pb.txt"
        write_server_config_templage(args.server_config, args)

    # Generate nginx config if not specified
    nginx_conf = args.nginx_config
    if nginx_conf is None:
        ingress = make_ingress(args)
        nginx_conf = args.config_dir + "/nginx.conf"
        ensure(args.config_dir)
        write_template(ingress, nginx_conf, args)

    # Start NGINX
    start_nginx(args.nginx, nginx_conf)
