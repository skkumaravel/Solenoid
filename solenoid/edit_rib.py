import json
import sys
import os
import ConfigParser
import argparse
from netaddr import IPNetwork
from netaddr import AddrFormatError
from jinja2 import Environment, PackageLoader
#This needs to be fixed with a setup.sh script.
#For now, users should uncomment this when running script as a daemon.
#Make sure to change the path to your location.
#sys.path.append('/home/cisco/exabgp/solenoid/')
from solenoid import JSONRestCalls
from logs.logger import Logger


_source = 'route-injector'
logger = Logger()


def render_config(update_json):
    """Take a exa command and translate it into yang formatted JSON

    :param update_json: The exa bgp string that is sent to stdout
    :type update_json: str

    """
    # Check if any filtering has been applied to the prefixes.
    try:
        if os.path.getsize(filepath) > 0:
            filt = True
        else:
            filt = False
    except OSError:
        filt = False
    # Render the config.
    try:
        update_type = update_json['neighbor']['message']['update']
        #Check if it is an announcement or withdrawal.
        if 'announce' in update_type:
            updated_prefixes = update_type['announce']['ipv4 unicast']
            #Grab the next hop value.
            next_hop = updated_prefixes.keys()[0]
            #Grab the list of prefixes.
            prefixes = updated_prefixes.values()[0].keys()

            # Filter the prefixes
            if filt:
                prefixes = filter_prefixes(prefixes)

            # set env variable for jinja2
            env = Environment(loader=PackageLoader('solenoid',
                                                   'templates'))
            env.filters['to_json'] = json.dumps
            template = env.get_template('static.json')
            rib_announce(template.render(next_hop=next_hop,
                                         prefixes=prefixes))
        elif 'withdraw' in update_type:
            exa_prefixes = update_type['withdraw']['ipv4 unicast'].keys()
            # Filter the prefixes
            if filt:
                exa_prefixes = filter_prefixes(prefixes)
            for withdrawn_prefix in exa_prefixes:
                rib_withdraw(withdrawn_prefix)
    except ValueError, e:  # If we hit an eor or other type of update
        logger.warning(e, _source)


def create_rest_object():
    """Create a restCalls object.
        Reads in a file containing username, password, and
        ip address:port, in that order.

        This method could be eliminated and the restCalls(username, password,
        ip_address:port) replace all calls to create_rest_object().
        This method exists in order to seperate passwords from github.

        :returns: restCalls object
        :rtype: restCalls class object
    """
    try:
        # Can be absolute or relative.
        obj = os.environ['ROUTE_INJECT_CONFIG']
    except KeyError:
        logger.critical(
            'You must set the environment variable ROUTE_INJECT_CONFIG'
        )
        sys.exit(1)
    config = ConfigParser.ConfigParser()
    try:
        config.readfp(open(os.path.expanduser(obj)))
        return JSONRestCalls(
            config.get('default', 'ip'),
            config.get('default', 'port'),
            config.get('default', 'username'),
            config.get('default', 'password')
        )
    except (ConfigParser.Error, ValueError), e:
        logger.critical(
            'Something is wrong with your config file: {}'.format(
                e.message
            )
        )
        sys.exit(1)


def rib_announce(rendered_config):
    """Add networks to the RIB table using HTTP PATCH over RESTconf.

    :param rendered_config: Jinja2 rendered configuration file
    :type rendered_config: unicode

    """
    rest_object = create_rest_object()
    response = rest_object.patch(
        'Cisco-IOS-XR-ip-static-cfg:router-static',
        rendered_config
    )
    status = response.status_code
    if status in xrange(200, 300):
        logger.info('ANNOUNCE | {code}'.format(code=status), _source)
    else:
        logger.warning('ANNOUNCE | {code}'.format(code=status), _source)


def rib_withdraw(withdrawn_prefix):
    """Remove the withdrawn prefix from the RIB table.

        :param new_config: The prefix and prefix-length to be removed
        :type new_config: str
    """
    rest_object = create_rest_object()
    exa_prefix, prefix_length = withdrawn_prefix.split('/')
    url = 'Cisco-IOS-XR-ip-static-cfg:router-static/default-vrf/address-family/vrfipv4/vrf-unicast/vrf-prefixes/vrf-prefix={},{}'
    url = url.format(exa_prefix, prefix_length)
    response = rest_object.delete(url)
    status = response.status_code
    if status in xrange(200, 300):
        logger.info('ANNOUNCE | {code}'.format(code=status), _source)
    else:
        logger.warning('ANNOUNCE | {code}'.format(code=status), _source)


def filter_prefixes(prefixes):
    """Filters out prefixes that do not fall in ranges indicated in filter.txt

    :param prefixes: List of prefixes exaBGP announced or withdrew
    :type prefixes: list or strings

    """
    # TODO: Add the capability of only have 1 IP, not a range.
    with open(filepath) as filterf:
        final = []
        for line in filterf:
            temp_list = []
            try:
                # Convert it all to IPNetwork for comparison.
                ip1, ip2 = line.split('-')
                ip2 = ip2.strip()
                ip1 = IPNetwork(ip1)
                ip2 = IPNetwork(ip2)
                for prefix in prefixes:
                    prefix = IPNetwork(prefix)
                    # Is the exaBGP prefix in the filtering range?
                    if ip1 <= prefix <= ip2:
                        # If the item is already in the list, don't re-add it.
                        if str(prefix) in temp_list:
                            continue
                        else:
                            temp_list.append(str(prefix))
                # Create the final list.
                final += temp_list
            # Make this more specific.
            except AddrFormatError, e:
                logger.error('FILTER | {}'.format(e), _source)
                print e
            except ValueError, e:
                logger.error('FILTER | {}'.format(e.message), _source)
        return final


def update_watcher():
    """Watches for BGP updates from neighbors and triggers RIB change."""
    location = os.path.dirname(os.path.realpath(__file__))
    open(os.path.join(location, 'updates.txt')).close()
    while True:
        # Listen for BGP updates.
        raw_update = sys.stdin.readline().strip()
        try:
            update_json = json.loads(raw_update)
        except ValueError:
            logger.error('Failed JSON conversion for exa update', _source)
        else:
            try:
                # If it is an update, make the RIB changes.
                if update_json['type'] == 'update':
                    render_config(update_json)

                    # Add the change to the update file.
                    with open(os.path.join(location, 'updates.txt'), 'a') as f:
                        f.write(raw_update + '\n')
            except KeyError:
                logger.warning(
                    'Failed to find "update" keyword in exa update',
                    _source
                )

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-f', type=str)
    args = parser.parse_args()
    global filepath
    filepath = args.f
    update_watcher()
