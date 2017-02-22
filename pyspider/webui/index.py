#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Author: Binux<i@binux.me>
#         http://binux.me
# Created on 2014-02-22 23:20:39

import socket
import time
import datetime
from six import iteritems, itervalues
from flask import render_template, request, json

try:
    import flask_login as login
except ImportError:
    from flask.ext import login

from .app import app

index_fields = ['name', 'group', 'status', 'comments', 'rate', 'burst', 'updatetime']


@app.route('/')
def index():
    projectdb = app.config['projectdb']
    projects = sorted(projectdb.get_all(fields=index_fields),
                      key=lambda k: (0 if k['group'] else 1, k['group'] or '', k['name']))
    return render_template("index.html", projects=projects)


@app.route('/queues')
def get_queues():
    def try_get_qsize(queue):
        if queue is None:
            return 'None'
        try:
            return queue.qsize()
        except Exception as e:
            return "%r" % e

    result = {}
    queues = app.config.get('queues', {})
    for key in queues:
        result[key] = try_get_qsize(queues[key])
    return json.dumps(result), 200, {'Content-Type': 'application/json'}


@app.route('/update', methods=['POST', ])
def project_update():
    projectdb = app.config['projectdb']
    project = request.form['pk']
    name = request.form['name']
    value = request.form['value']

    project_info = projectdb.get(project, fields=('name', 'group'))
    if not project_info:
        return "no such project.", 404
    if 'lock' in projectdb.split_group(project_info.get('group')) \
            and not login.current_user.is_active():
        return app.login_response

    if name not in ('group', 'status', 'rate'):
        return 'unknown field: %s' % name, 400
    if name == 'rate':
        value = value.split('/')
        if len(value) != 2:
            return 'format error: rate/burst', 400
        rate = float(value[0])
        burst = float(value[1])
        update = {
            'rate': min(rate, app.config.get('max_rate', rate)),
            'burst': min(burst, app.config.get('max_burst', burst)),
        }
    else:
        update = {
            name: value
        }

    ret = projectdb.update(project, update)
    if ret:
        rpc = app.config['scheduler_rpc']
        if rpc is not None:
            try:
                rpc.update_project()
            except socket.error as e:
                app.logger.warning('connect to scheduler rpc error: %r', e)
                return 'rpc error', 200
        return 'ok', 200
    else:
        return 'update error', 500


@app.route('/counter')
def counter():
    rpc = app.config['scheduler_rpc']
    if rpc is None:
        print(11111111111111111)
        return json.dumps({})

    result = {}
    try:
        data = rpc.webui_update()
        for type, counters in iteritems(data['counter']):
            for project, counter in iteritems(counters):
                result.setdefault(project, {})[type] = counter
        for project, paused in iteritems(data['pause_status']):
            result.setdefault(project, {})['paused'] = paused
    except socket.error as e:
        app.logger.warning('connect to scheduler rpc error: %r', e)
        return json.dumps({}), 200, {'Content-Type': 'application/json'}

    return json.dumps(result), 200, {'Content-Type': 'application/json'}


@app.route('/run', methods=['POST', ])
def runtask():
    rpc = app.config['scheduler_rpc']
    if rpc is None:
        return json.dumps({})

    projectdb = app.config['projectdb']
    project = request.form['project']
    project_info = projectdb.get(project, fields=('name', 'group'))
    if not project_info:
        return "no such project.", 404
    if 'lock' in projectdb.split_group(project_info.get('group')) \
            and not login.current_user.is_active():
        return app.login_response

    newtask = {
        "project": project,
        "taskid": "on_start",
        "url": "data:,on_start",
        "process": {
            "callback": "on_start",
        },
        "schedule": {
            "age": 0,
            "priority": 9,
            "force_update": True,
        },
    }

    try:
        ret = rpc.newtask(newtask)
    except socket.error as e:
        app.logger.warning('connect to scheduler rpc error: %r', e)
        return json.dumps({"result": False}), 200, {'Content-Type': 'application/json'}
    return json.dumps({"result": ret}), 200, {'Content-Type': 'application/json'}


@app.route('/robots.txt')
def robots():
    return """User-agent: *
Disallow: /
Allow: /$
Allow: /debug
Disallow: /debug/*?taskid=*
""", 200, {'Content-Type': 'text/plain'}


@app.route('/health')
def health():
    """
    health check, service:
    webui
    scheduler
    fetcher
    processor
    result_worker
    :return:
    """
    # all service return dict
    good = {
        "status": "OK",
        "type": "HARD",
        "checked_at": gettime(),
        "spent_time": "1ms",
        "info": "ok"
    }
    bad = {
        "status": "ERROR",
        "type": "HARD",
        "checked_at": gettime(),
        "spent_time": "1ms",
        "info": "ERROR"
    }

    _health = {
        "status": "OK",
        "dependencies": {
            "webui": good.copy()
        }
    }

    # scheduler fetcher procsssor result
    mapping = {
        'fetcher2processor': 'processor',
        'newtask_queue': 'scheduler',
        'processor2result': 'result_worker',
        'scheduler2fetcher': 'fetcher'
    }

    result = get_queues()
    queue_info = dict(json.loads(result[0]))
    # redis down
    try:
        alert_info = {k: int(v) for k, v in queue_info.items()}
        _health['dependencies']['redis'] = good.copy()
    except:
        _health['dependencies']['redis'] = bad.copy()
        _health['dependencies']['redis']['info'] = 'redis down'
        return json.dumps(_health)

    # check num
    for k, v in alert_info.items():
        service = mapping.get(k)
        if service:
            if v >= 100:
                _health['dependencies'][service] = bad.copy()
                _health['dependencies'][service]['info'] = '{} down'.format(service)
            else:
                _health['dependencies'][service] = good.copy()

    # check error table
    resultdb = app.config['resultdb']
    project = 'error'
    offset = int(request.args.get('offset', 0))
    limit = int(request.args.get('limit', 20))
    try:
        results = list(resultdb.select(project, offset=offset, limit=limit))
        _health['dependencies']['db'] = good.copy()
    except:
        # db down
        _health['dependencies']['db'] = bad.copy()
        _health['dependencies']['db']['info'] = 'db down'

    # scheduler xmlrpc detect
    try:
        app.config['scheduler_rpc_client'].ping()
    except:
        _health['dependencies']['scheduler']['status'] = 'ERROR'
        _health['dependencies']['scheduler']['info'] = 'scheduler server down'

    try:
        app.config['fetcher_rpc_client'].ping()
    except:
        _health['dependencies']['fetcher']['status'] = 'ERROR'
        _health['dependencies']['fetcher']['info'] = 'fetcher server down'

    return json.dumps(_health)


@app.route('/ping')
def ping():
    return 'pong'


def gettime():
    return datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S') + str(time.time() % 1)[1:5] + 'Z'
