from lab_scopes.transports.lecroy_vicp import (
    HEADER_SIZE,
    OP_DATA,
    OP_EOI,
    decode_vicp_header,
    encode_vicp_message,
)


def test_vicp_encode_decode_header():
    frame = encode_vicp_message("COMM_HEADER OFF\n", sequence=7)
    header = decode_vicp_header(frame[:HEADER_SIZE])

    assert header["sequence"] == 7
    assert header["payload_len"] == len(b"COMM_HEADER OFF\n")
    assert header["operation"] & OP_DATA
    assert header["operation"] & OP_EOI
    assert frame[HEADER_SIZE:] == b"COMM_HEADER OFF\n"
