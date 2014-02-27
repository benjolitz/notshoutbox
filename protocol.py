import struct
import socket
import itertools
import collections

class ConnectionClosed(socket.error):
    pass

READ_BEGIN = 1
READ_BODY = 2

def split_hixie_chunks(chunks, state=None):
    '''
    >>> test = "\\xffHello\\x00\\xffWorld"
    >>> msgs = []
    >>> for msg in split_hixie_chunks(test):
    ...     if isinstance(msg, STATE):
    ...         break
    ...     msgs.append(msg)
    >>> if msgs:
    ...     test = test[msgs[-1].trim_index:]
    >>> test
    '\\xffWorld'
    >>> test += "\\x00\\xffOk?\\x00"
    >>> for msg in split_hixie_chunks(test, msg):
    ...     if isinstance(msg, STATE):
    ...         break
    ...     msgs.append(msg)
    >>> if msgs:
    ...     test = test[msgs[-1].trim_index:]
    >>> test
    ''
    >>> [x.msg for x in msgs]
    ['Hello', 'World', 'Ok?']

    And if a closer comes in?
    >>> test = '\\xff\\x00'
    >>> for msg in split_hixie_chunks(test):
    ...     if isinstance(msg, STATE):
    ...         break
    ...     msgs.append(msg)
    Traceback (most recent call last):
    ...
    ConnectionClosed
    '''
    last_mode = READ_BEGIN
    buf = []
    trim_index = 0
    for idx, char in enumerate(itertools.chain(*chunks)):
        if idx != trim_index:
            continue
        trim_index += 1
        buf.append(char)
        if last_mode == READ_BEGIN:
            if buf[0] == '\xff':
                last_mode = READ_BODY
                buf[:] = []
                continue
        if last_mode == READ_BODY:
            if char == '\x00':
                buf.pop()
                r = ''.join(buf)
                if b'' == r:
                    raise ConnectionClosed()
                yield RESULT(r, trim_index)
                buf[:] = []
                last_mode = READ_BEGIN
    if buf:
        yield STATE(last_mode, buf, None, None, None, trim_index)



RESULT = collections.namedtuple("Result", "msg trim_index")
STATE = collections.namedtuple("PausedState", "last_mode buf lenght_to_read masks index trim_index")

GET_FRAME_TYPE = 1
GET_LENGTH_2 = 2
GET_LENGTH_8 = 4
GET_MASKS = 8
GET_CONTENT = 16


TEXT = 1
BINARY = 2
def split_rfc_chunks(chunks, state=None):
    '''
    >>> test = str(bytearray([0x81, 0x85, 0x37, 0xfa, 0x21, 0x3d, 0x7f, 0x9f, 0x4d, 0x51, 0x58]))
    >>> for i in split_rfc_chunks(test):
    ...     print(tuple(i))
    ('Hello', 11)
    >>> test = test[11:]
    >>> test
    ''

    Handle partial states:
    >>> msgs = []
    >>> test = str(bytearray([0x81, 0x85, 0x37, 0xfa, 0x21, 0x3d, 0x7f, 0x9f, 0x4d, 0x51]))
    >>> for msg in split_rfc_chunks(test):
    ...     if isinstance(msg, STATE):
    ...         break
    ...     msgs.append(msg)
    >>> msgs
    []
    >>> msg
    PausedState(last_mode=16, buf=['H', 'e', 'l', 'l'], lenght_to_read=5, masks=[55, 250, 33, 61], index=4, trim_index=10)
    >>> test += 'X'
    >>> for msg in split_rfc_chunks(test, msg):
    ...    if isinstance(msg, STATE):
    ...         break
    ...    msgs.append(msg)
    >>> msgs
    [Result(msg='Hello', trim_index=11)]
    >>> test = test[msgs[-1][1]:]
    >>> test
    ''

    Two messages and chunk?
    >>> test = str(bytearray([0x81, 0x85, 0x37, 0xfa, 0x21, 0x3d, 0x7f, 0x9f, 0x4d, 0x51, 0x58])) * 2
    >>> test += str(bytearray([0x81, 0x85]))
    >>> msgs = []
    >>> trim_chunks = False
    >>> for msg in split_rfc_chunks(test):
    ...     if isinstance(msg, STATE):
    ...        last_state = msg
    ...        trim_chunks = False
    ...        break
    ...     msgs.append(msg)
    ... else:
    ...     trim_chunks = True
    >>> [x.msg for x in msgs] == ['Hello', 'Hello']
    True
    >>> if trim_chunks:
    ...     test = test[msgs[-1].trim_index:]

    So we got the first two messages... What about the last coming in slooowly?
    >>> test += str(bytearray([0x37, 0xfa, 0x21, 0x3d, 0x7f,]))

    Reset your trim command and go:
    >>> for msg in split_rfc_chunks(test):
    ...     if isinstance(msg, STATE):
    ...        trim_chunks = False
    ...        last_state = msg
    ...        break
    ...     msgs.append(msg)
    ... else:
    ...     trim_chunks = True
    >>> test_length = len(test)
    >>> if trim_chunks:
    ...     test = test[msgs[-1].trim_index:]
    >>> test_length == len(test)
    True

    So we don't trim yet...
    >>> test += str(bytearray([0x9f, 0x4d, 0x51, 0x58]))
    >>> for msg in split_rfc_chunks(test):
    ...     if isinstance(msg, STATE):
    ...        last_state = msg
    ...        trim_chunks = False
    ...        break
    ...     msgs.append(msg)
    ... else:
    ...     trim_chunks = True
    >>> test = test[msgs[-1].trim_index:]
    >>> [x.msg for x in msgs]
    ['Hello', 'Hello', 'Hello']
    >>> test
    ''
    '''
    if state:
        last_mode = state.last_mode
        buf = state.buf
        lenght_to_read = state.lenght_to_read
        masks = state.masks
        index = state.index
        trim_index = state.trim_index
    else:
        last_mode = GET_FRAME_TYPE
        buf = []
        lenght_to_read = None
        masks = [0, 0, 0, 0]
        index = 0
        trim_index = 0
    for idx, char in enumerate(itertools.chain(*chunks)):
        if idx != trim_index:
            continue
        trim_index += 1
        buf.append(char)
        if last_mode == GET_FRAME_TYPE:
            if len(buf) == 2:
                frame_type = ord(buf[0]) & 127
                if 8 == frame_type:
                    raise ConnectionClosed()
                elif frame_type in (TEXT, BINARY):
                    length = ord(buf[1]) & 127
                    if 126 == length:
                        last_mode = GET_LENGTH_2
                    elif 127 == length:
                        last_mode = GET_LENGTH_8
                    else:
                        lenght_to_read = length
                        last_mode = GET_MASKS
                    buf[:] = []
                    # yield?
                    continue
        if last_mode in (GET_LENGTH_2, GET_LENGTH_8):
            if last_mode == GET_LENGTH_2 and len(buf) == 2:
                lenght_to_read = struct.unpack(b">H", ''.join(buf))[0]
                last_mode = GET_MASKS
            if last_mode == GET_LENGTH_8 and len(buf) == 8:
                lenght_to_read = struct.unpack(b">Q", ''.join(buf))[0]
                last_mode = GET_MASKS
            if last_mode == GET_MASKS:
                buf[:] = []
                continue
        if last_mode == GET_MASKS:
            masks[len(buf)-1] = ord(buf[-1])
            if len(buf) == 4:
                last_mode = GET_CONTENT
                buf[:] = []
                continue
        if last_mode == GET_CONTENT:
            buf[-1] = chr(ord(char) ^ masks[index % 4])
            index += 1
            if index == lenght_to_read:
                yield RESULT(''.join(buf), trim_index)
                last_mode = GET_FRAME_TYPE
                index = 0
                buf[:] = []
    if buf:
        yield STATE(last_mode, buf, lenght_to_read, masks, index, trim_index)

if __name__ == "__main__":
    import doctest
    doctest.testmod()