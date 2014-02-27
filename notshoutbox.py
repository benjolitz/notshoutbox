#!/usr/bin/env python
import time
import protocol
import socket
import select
import collections
import json
import itertools
import hashlib
from base64 import b64encode
from mimetools import Message
try:
    import __pypy__
except ImportError:
    try:
        from cStringIO import StringIO
    except ImportError:
        StringIO = None
else:
    StringIO = None

if StringIO is None:
    from StringIO import StringIO
BUF_SIZE = 24
ACCEPT_CLIENT_MSG = "Accepting client from {0} on port {1}"

def handle_socket_error(excc):
    if excc.errno == 114:
        return
    if excc.errno == 11:
        return
    raise

def coroutine(func):
    def start(*args,**kwargs):
        cr = func(*args,**kwargs)
        cr.next()
        return cr
    return start

class History(object):
    def __init__(self, limit=50, new_client_backlog=5):
        self.sequence_no = 1
        self.limit = limit
        self.storage = collections.deque()
        self.in_storage = 0
        self.new_client_backlog = new_client_backlog

    def append(self, msg):
        self.storage.appendleft((self.sequence_no, msg,))
        self.sequence_no += 1
        if self.in_storage < self.limit:
            self.in_storage += 1
        if self.in_storage == self.limit:
            self.pop()

    def new_client(self):
        return HistoryPtr(self, self.sequence_no, self.new_client_backlog)

class HistoryPtr(object):
    def __init__(self, history, seq_no, backlog):
        self.origin = seq_no
        self.backlog = backlog
        self.history = history.storage

    def __iter__(self):
        counter = self.backlog
        for msg in self.history:
            if msg[0] > self.origin:
                continue
            if not counter:
                break
            yield msg[1]
            counter -= 1


class Connection(object):
    RFC_MAGIC_HASH = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
    HIXIE_WEBSOCKET_SEC_KEY_ORDER = \
            (b'Sec-WebSocket-Key1', b'Sec-WebSocket-Key2',)

    WAITING_FOR_HANDSHAKE = 1
    READ_WHEN = 2
    ACCEPT_MORE = 4

    WEBSOCKET_CLIENT = 0xfffff

    'This function is assigned at instantiation time'
    _do_handshake = None

    def __init__(self, sock, address, loop, state=None, history=None, socket_type=None):
        self.socket = sock
        self._state = 0
        self.socket.setblocking(0)
        self.loop = loop
        self.address = address
        self.type = socket_type
        self.state = state or -1
        self.history = history
        self._buffer = []
        self.last_state = None

    @property
    def type(self):
        return self._type

    @type.setter
    def type(self, x):
        # if Connection.WEBSOCKET_CLIENT == x:
        self.do_handshake = self._websocket_client_handshake

        # if self._do_handshake is None:
        #     self._do_handshake = self._no_op_handshake
        #     self._decode_message = self._identity_decode

        self._type = x

    def _recv(self, buf):
        data = self.socket.recv(buf)
        if data == '':
            raise protocol.ConnectionClosed()
        return data

    def _websocket_client_handshake(self):
        buf = []
        while True:
            fds = yield
            if self.socket in fds:
                try:
                    data = self._recv(BUF_SIZE)
                except socket.error as e:
                    handle_socket_error(e)
                else:
                    buf.append(data)
                    score = 0
                    for char in itertools.chain(*buf):
                        if char in ('\r', '\n'):
                            score += 1
                            if score == 4:
                                break
                        else:
                            score = 0
                    else:
                        continue
                    break
        header, body = ''.join(buf).split('\r\n\r\n', 1)

        headers = Message(StringIO(header.split(b'\r\n', 1)[1]))
        if headers.get(b'Upgrade', '').lower() != b'websocket':
            raise protocol.ConnectionClosed()
        # common headers:
        response = b''.join(
            (b'HTTP/1.1 101 Switching Protocols\r\n',
             b'Upgrade: websocket\r\n',
             b'Connection: Upgrade\r\n'))
        # if override_host:
        #     headers[b'host'] = override_host

        if b'Sec-WebSocket-Key1' in headers and \
                b'Sec-WebSocket-Key2' in headers:
            self._decode_message = self._decode_hixie
            accept = hashlib.md5(
                b''.join((struct.pack(b'>II', *list(
                    int(b''.join(x for x in headers[key]
                        if x in string.digits), 10) / headers[key].count(b' ')
                    for key in self.HIXIE_WEBSOCKET_SEC_KEY_ORDER)
                ), body,))).digest()
            response = b''.join(
                (response,
                 b'Sec-WebSocket-Origin: %s\r\n' % headers[b'origin'],
                 b'Sec-WebSocket-Location: ws://%s/\r\n' % headers[b'host'],
                 b'\r\n%s' % str(accept),
                 ))
            self._send(response)
            self._send = self._send_hixie
        else:
            self._decode_message = self._decode_rfc
            secret_key = \
                b''.join((headers[b'Sec-WebSocket-Key'], self.RFC_MAGIC_HASH,))
            digest = b64encode(
                hashlib.sha1(
                    secret_key).hexdigest().decode(b'hex')
            )
            response = \
                b''.join((response, b'Sec-WebSocket-Accept: %s\r\n\r\n' % digest,))
            self._send(response)
            self._send = self._send_rfc
        print("Ready for reading.")
        self.state = self.READ_WHEN

    def _send(self, msg):
        self.socket.send(msg)

    def _send_rfc(self, message):
        self.socket.send(chr(129))
        length = len(message)
        if length <= 125:
            self.socket.send(chr(length))
        elif length >= 126 and length <= 65535:
            self.socket.send(126)
            self.socket.send(struct.pack(b">H", length))
        else:
            self.socket.send(127)
            self.socket.send(struct.pack(b">Q", length))
        self.socket.send(message)

    def _send_hixie(self, message):
        self.socket.send('\xff' + message + '\x00')

    def on_message(self, msg):
        self.history.append(msg)
        data = json.loads(msg)
        if 'cmd' in data:
            self.my_history_ptr = self.history.new_client()
            for msg in reversed(list(self.my_history_ptr)):
                self._send(msg)
        else:
            for conn in (x for x in self.loop if x is not self and x.state == Connection.READ_WHEN):
                conn._send(msg)

    def _decode_rfc(self):
        messages = []
        for msg in protocol.split_rfc_chunks(self._buffer, self.last_state):
            if isinstance(msg, protocol.STATE):
                if messages and messages[-1]:
                    self._buffer = [''.join(self._buffer)[messages[-1].trim_index:]]
                self.last_state = msg
                break
            messages.append(msg)
        else:
            self.last_state = None
            self._buffer = []
        yield messages

    def _decode_hixie(self):
        messages = []
        for msg in protocol.split_hixie_chunks(self._buffer):
            if isinstance(msg, protocol.STATE):
                if messages and messages[-1]:
                    self._buffer = [''.join(self._buffer)[messages[-1].trim_index:]]
                break
            messages.append(msg)
        else:
            self.last_state = None
            self._buffer = []
        yield messages

    @coroutine
    def accept(self):
        while True:
            fds = yield
            if self.socket in fds:
                sock, address = self.socket.accept()
                print(
                    ACCEPT_CLIENT_MSG.format(*address))
                new_client = \
                    Connection(
                        sock, address,
                        self.loop, history=self.history)
                self.loop.append(new_client)


    @coroutine
    def read_when(self):
        fds = yield
        if self.socket in fds:
            try:
                data = self._recv(BUF_SIZE)
            except socket.error as e:
                handle_socket_error(e)
            else:
                self._buffer.append(data)
                for msgs in self._decode_message():
                    [self.on_message(result.msg) for result in msgs]
        self.state = self.READ_WHEN


    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, x):
        if x == self.WAITING_FOR_HANDSHAKE:
            self.target = self.do_handshake()
            self.target.next()
        if x == self.READ_WHEN:
            self.target = self.read_when()
        if x == self.ACCEPT_MORE:
            self.target = self.accept()

        if -1 == x:
            self.state = self.WAITING_FOR_HANDSHAKE
        self._state = x

    def send(self, *args):
        try:
            self.target.send(*args)
        except protocol.ConnectionClosed:
            print("Removing self")
            self.loop.remove(self)

class Loop(object):
    def __init__(self):
        self._loop = []
        self.fds = []

    def append(self, obj):
        self._loop.append(obj)
        self.fds.append(obj.socket)

    def remove(self, obj):
        self.fds.remove(obj.socket)
        self._loop.remove(obj)

    def __iter__(self):
        for i in self._loop:
            yield i


def get_poller():
    try:
        from select import epoll as poll
    except ImportError:
        try:
            from select import poll
        except ImportError:
            poll = None
    if poll:
        lut = {}
        lut_back = {}
        poller = poll()
        def poll_it(fds, delay):
            for fd in fds:
                if fd not in lut:
                    lut_back[fd.fileno()] = fd
                    lut[fd] = fd.fileno()
                    poller.register(fd)
            return [lut_back[x[0]] for x in poller.poll(delay)]
        return poll_it
    def poll_it(fds, delay):
        return select.select(fds, [], [], delay)[0]
    return poll_it


if __name__ == "__main__":
    _poller = get_poller()

    listener_socket = socket.socket()
    listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener_socket.bind(('', 8080))
    listener_socket.listen(50)
    loop = Loop()
    a = Connection(listener_socket, "NUL", loop=loop, state=Connection.ACCEPT_MORE)
    a.history = History(limit=50)
    loop.append(a)
    index = 0
    last = time.time()

    while True:
        fds = _poller(loop.fds, 0.01)
        for item in loop:
            try:
                item.send(fds)
            except StopIteration:
                pass
        if time.time() - last > 2:
            last = time.time()
            print("pulse.")
