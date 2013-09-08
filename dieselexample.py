import cgi

from diesel import first, Loop

from diesel.web import DieselFlask
from diesel.protocols.websockets import WebSocketDisconnect, WebSocketServer

from diesel.protocols.redis import RedisSubHub, RedisClient
from simplejson import dumps, loads, JSONDecodeError
import collections
from flask import request

MESSAGES_TO_GET = 5

#f = Fanout()



def monkey_patch_websocket(self, no_web=None):
    if not no_web:
        def no_web(req):
            '''Websocket failsafe!'''
            assert 0, "Failed!"
    def real_decorator(f):
        ws = WebSocketServer(no_web, f)
        def ws_call(*args, **kw):
            assert not args and not kw, "No arguments allowed to websocket routes"
            return ws.do_upgrade(request)
        return ws_call
    return real_decorator

DieselFlask.websocket = monkey_patch_websocket

app = DieselFlask(__name__)

hub = RedisSubHub(host="localhost")

@app.route("/")
def web_handler():
    with open("chat.html") as fh:
        content = fh.read()
    return content

@app.route("/jquery.gracefulWebSocket.js")
def getsocketshim():
    with open("jquery.gracefulWebSocket.js") as fh:
        content = fh.read()
    return content

chat_log = None
def log_message(msg):
    global chat_log
    if chat_log is None:
        chat_log = hub.make_client()
    chat_log.rpush('chat_logs', msg)


def last_messages():
    global chat_log
    if chat_log is None:
        chat_log = hub.make_client()
    with chat_log.transaction() as t:
        for i in reversed(xrange(1, MESSAGES_TO_GET+1)):
            chat_log.lindex('chat_logs', -i)
    for oldmessage in (val for val in t.value if val is not None):
        yield oldmessage

def add_message(msg):
    global chat_log
    if chat_log is None:
        chat_log = hub.make_client()
    chat_log.rpush('chat_logs', msg)


def websocket_failsafe(req):
    global chat_log
    print req.args
    if req.form:
        try:
            items = loads(req.form.keys()[0])
        except ValueError as e:
            print e
            items = {}
        cmd = items.get("cmd", "")
        if cmd == "":
            c = hub.make_client()
            msg = dumps({
                    'nick' : cgi.escape(items.get('nick', '').strip()),
                    'message' : cgi.escape(items.get('message', '').strip()),
                })
            subs = c.publish("chat_channel", msg)
            print "published message to %i subscribers" % subs
            add_message(msg)
        elif cmd == "getList":
            return dumps({'state': [x for x in last_messages()]})
    start_time = req.args.get('previousRequest')
    stop_time = req.args.get('currentRequest')
    print start_time, stop_time
    return '{}'

@app.route("/ws",  methods=['GET', 'POST'])
@app.websocket(websocket_failsafe)
def pubsub_socket(req, inq, outq):
    c = hub.make_client()
    with hub.subq('chat_channel') as group:
        while True:
            q, v = first(waits=[inq, group])
            if q == inq: # getting message from client
                print "(inq) %s" % v
                cmd = v.get("cmd", "")
                print "cmd is %s" % cmd
                if cmd=="":
                    msg = dumps({
                        'nick' : cgi.escape(v['nick'].strip()),
                        'message' : cgi.escape(v['message'].strip()),
                    })
                    subs = c.publish("chat_channel", msg)
                    print "published message to %i subscribers" % subs
                    chat_log.rpush('chat_logs', msg)
                elif cmd == "getList":
                    for message in last_messages():
                        outq.put(loads(message))
                    # outq.put(dict(message="test bot"))

            elif q == group: # getting message for broadcasting
                chan, msg_str = v
                try:
                    msg = loads(msg_str)
                    data = dict(message=msg['message'], nick=msg['nick'])
                    print "(outq) %s" % data
                    outq.put(data)
                except JSONDecodeError:
                    print "error decoding message %s" % msg_str
            elif isinstance(v, WebSocketDisconnect): # getting a disconnect signal
                return
            else:
                print "oops %s" % v

app.diesel_app.add_loop(Loop(hub))
app.run()