#! /usr/bin/env python

import json
import time
import os
import socket
import pprint
import signal
import sys
from optparse import OptionParser
from multiprocessing import Pool
import select

import boto.ec2
import paramiko


STATE_FILENAME = os.path.abspath(os.path.dirname(__file__)) + '/' + '.status.json'
CONFIG_FILENAME = os.path.abspath(os.path.dirname(__file__)) + '/' + 'config.json'


class STATE():
    STOPPED = 1
    BOOTED = 2


def _create_ec2_connection(region):
    return boto.ec2.connect_to_region(
        region_name=region,
        aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
    )


def _update_state(state_dict):
    with open(STATE_FILENAME, 'w') as fl:
        json.dump(state_dict, fl)


def _read_state():
    if not os.path.isfile(STATE_FILENAME):
        return {
            'state': STATE.STOPPED,
        }

    with open(STATE_FILENAME, 'r') as fl:
        return json.load(fl)


def _read_config():
    if not os.path.isfile(CONFIG_FILENAME):
        raise Exception("%s does not exist!" % CONFIG_FILENAME)

    with open(CONFIG_FILENAME, 'r') as fl:
        conf = json.load(fl)

    return conf


def run(slave_num=1):
    before_state = _read_state()
    if before_state['state'] != STATE.STOPPED:
        raise Exception('Instances are already registered!')

    if slave_num < 1:
        raise Exception('Invalid slave num')

    conf = _read_config()

    instance_num = 1 + slave_num
    print 'Launching master instance x 1 + slave instances x %s' % slave_num

    region = conf['aws']['region']
    ami_id = conf['aws']['ami']
    placement = conf['aws']['placement']
    subnet_id = conf['aws']['subnet_id']
    groups = conf['aws']['groups']

    conn = _create_ec2_connection(region)

    # create instances
    interface = boto.ec2.networkinterface.NetworkInterfaceSpecification(
        subnet_id=subnet_id,
        groups=groups,
        associate_public_ip_address=True
    )
    interfaces = boto.ec2.networkinterface.NetworkInterfaceCollection(interface)

    reservation = conn.run_instances(
        image_id=ami_id,
        min_count=instance_num,
        max_count=instance_num,
        key_name=conf['instance']['key_name'],
        instance_type=conf['instance']['type'],
        placement=placement,
        # subnet_id='subnet-d8d7e09e',
        # security_group_ids=['sg-f2278297', 'sg-892d88ec'],
        network_interfaces=interfaces,
    )

    master_instance = None
    slave_instances = []
    for instance in reservation.instances:
        while instance.update() != 'running':
            print '.'
            time.sleep(5)

        if master_instance is None:
            master_instance = instance
        else:
            slave_instances.append(instance)

        ins_id = instance.id
        print 'instance %s is up and running' % ins_id

    if master_instance is None:
        raise Exception("Master instance is not found!")

    if len(slave_instances) == 0:
        raise Exception("Slave instances are not found!")

    master_instance.add_tag('Name', 'jmeter - master')
    for instance in slave_instances:
        instance.add_tag('Name', 'jmeter - slave')

    state = {
        'state': STATE.BOOTED,
        'master': {
            'id': master_instance.id,
            'ip_address': master_instance.ip_address,
            'private_ip_address': master_instance.private_ip_address,
        },
        'slaves': []
    }

    for instance in slave_instances:
        state['slaves'].append({
            'id': instance.id,
            'ip_address': instance.ip_address,
            'private_ip_address': instance.private_ip_address,
        })

    _update_state(state)


def terminate():
    state = _read_state()
    if state['state'] != STATE.BOOTED:
        raise Exception('Instances are not registered!')

    conf = _read_config()
    region = conf['aws']['region']

    conn = _create_ec2_connection(region)

    instance_ids = [state['master']['id']]
    print 'Stopping master:%s' % state['master']['id']
    for ins_info in state['slaves']:
        instance_ids.append(ins_info['id'])
        print 'Stopping slave:%s' % ins_info['id']

    terminated_instances = conn.terminate_instances(instance_ids=instance_ids)

    state['state'] = STATE.STOPPED
    state.pop('master', None)
    state.pop('slaves', None)

    _update_state(state)


def report():
    state = _read_state()
    if state['state'] != STATE.BOOTED:
        print 'No instances running'
        return

    print 'master id:%s ip:%s' % (state['master']['id'], state['master']['ip_address'])

    for ins_info in state['slaves']:
        print 'slave id:%s ip:%s private_ip:%s' % (ins_info['id'], ins_info['ip_address'], ins_info['private_ip_address'])


def server():
    state = _read_state()
    if state['state'] != STATE.BOOTED:
        raise Exception('Instances are not registered!')

    conf = _read_config()
    region = conf['aws']['region']

    instance_ids = [ins_info['id'] for ins_info in state['slaves']]
    conn = _create_ec2_connection(region)

    reservations = conn.get_all_instances(instance_ids=instance_ids)
    instances = []

    for reservation in reservations:
        instances.extend(reservation.instances)

    params = []
    for i, instance in enumerate(instances):
        params.append({
            'i': i,
            'ip_address': instance.ip_address,
            'command': 'pkill java; cd /var/app/jmeter/bin; ./jmeter-server -Djava.rmi.server.hostname=%s > out.log 2>&1'
                       % instance.private_ip_address,
            # 'command': 'pkill java',
            'username': conf['instance']['username'],
            'key_name': conf['instance']['key_name'],
        })

    # pprint.pprint(params)

    pool = Pool(len(params))
    results = pool.map_async(_execute, params).get(99999999999)

    print 'Execute done'
    pprint.pprint(results)


def _get_pem_path(key):
    return os.path.expanduser('~/.ssh/%s.pem' % key)


def _execute(param):
    print '%i is starting..' % param['i']

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            param['ip_address'],
            username=param['username'],
            key_filename=_get_pem_path(param['key_name'])
        )

        print '%i is getting ready.. %s' % (param['i'], param['command'])

        command = param['command']
        stdin, stdout, stderr = client.exec_command(command)

        response = ''
        # response = stdout.read()
        # print response

        # error = ''
        # error = stderr.read()
        # print error

        if type(response) != str and type(response) != unicode:
            print '%i (connection timed out).' % param['i']
            return None

        client.close()

        return response
    except socket.error, e:
        pprint.pprint(e)
        return e


def master(jmx_file):
    state = _read_state()
    if state['state'] != STATE.BOOTED:
        raise Exception('Instances are not registered!')

    upload(jmx_file)

    conf = _read_config()

    private_ips = [ins_info['private_ip_address'] for ins_info in state['slaves']]
    command = 'pkill java; cd /var/app/jmeter/bin; ./jmeter -n -t loadtest.jmx -l result.jtl -R %s > out.log 2>&1'\
              % ','.join(private_ips)
    print 'Execute: %s' % command

    param = {
        'ip_address': state['master']['ip_address'],
        'username': conf['instance']['username'],
        'key_name': conf['instance']['key_name'],
    }

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        param['ip_address'],
        username=param['username'],
        key_filename=_get_pem_path(param['key_name'])
    )

    stdin, stdout, stderr = client.exec_command(command)
    # while not stdout.channel.exit_status_ready():
    #     # Only print data if there is data to read in the channel
    #     if not stdout.channel.recv_ready():
    #         continue
    #
    #     rl, wl, xl = select.select([stdout.channel], [], [], 0.0)
    #     if len(rl) <= 0:
    #         continue
    #     # Print data from stdout
    #     sys.stdout.write(stdout.channel.recv(1024))

    client.close()


def stop_master():
    state = _read_state()
    if state['state'] != STATE.BOOTED:
        raise Exception('Instances are not registered!')

    conf = _read_config()

    command = 'pkill java'
    print 'Execute: %s' % command

    param = {
        'ip_address': state['master']['ip_address'],
        'username': conf['instance']['username'],
        'key_name': conf['instance']['key_name'],
    }

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        param['ip_address'],
        username=param['username'],
        key_filename=_get_pem_path(param['key_name'])
    )

    stdin, stdout, stderr = client.exec_command(command)
    # while not stdout.channel.exit_status_ready():
    #     # Only print data if there is data to read in the channel
    #     if not stdout.channel.recv_ready():
    #         continue
    #
    #     rl, wl, xl = select.select([stdout.channel], [], [], 0.0)
    #     if len(rl) <= 0:
    #         continue
    #     # Print data from stdout
    #     sys.stdout.write(stdout.channel.recv(1024))

    client.close()


def upload(localfilepath):
    state = _read_state()
    if state['state'] != STATE.BOOTED:
        raise Exception('Instances are not registered!')

    conf = _read_config()

    param = {
        'ip_address': state['master']['ip_address'],
        'username': conf['instance']['username'],
        'key_name': conf['instance']['key_name'],
    }

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            param['ip_address'],
            username=param['username'],
            key_filename=_get_pem_path(param['key_name'])
        )

        transport = client.get_transport()
        sftp = paramiko.SFTPClient.from_transport(transport)
        localpath = localfilepath
        remotepath = '/var/app/jmeter/bin/loadtest.jmx'
        sftp.put(localpath, remotepath)
        return None
    except socket.error, e:
        pprint.pprint(e)
        return e
    finally:
        sftp.close()
        transport.close()


def show_ssh_command():
    state = _read_state()
    if state['state'] != STATE.BOOTED:
        raise Exception('Instances are not registered!')

    conf = _read_config()
    print 'ssh -o StrictHostKeyChecking=no -i %s -l %s %s' % (
        _get_pem_path(conf['instance']['key_name']),
        conf['instance']['username'],
        state['master']['ip_address']
    )


def main():
    parser = OptionParser()

    parser.add_option('-a', '--action', dest='action', help='action: run / terminate / report')
    parser.add_option('-n', '--num', dest='num', help='instance num')
    parser.add_option('-f', '--file', dest='jmx_file', help='jmx file')

    (opt, args) = parser.parse_args()

    jmx_file = opt.jmx_file or ''

    action = opt.action or ''
    if action == 'run':
        num = int(opt.num or 1)
        run(num)
    elif action == 'terminate':
        terminate()
    elif action == 'report':
        report()
    elif action == 'server':
        server()
    elif action == 'master':
        master(jmx_file)
    elif action == 'ssh':
        show_ssh_command()
    elif action == 'stop_master':
        stop_master()
    else:
        print 'no such action'


if __name__ == '__main__':
    main()
