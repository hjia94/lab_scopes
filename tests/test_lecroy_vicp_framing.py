from lab_scopes.transports.lecroy_vicp import (
    VICP_FRAME_HEADER_SIZE,
    OP_DATA,
    OP_EOI,
    decode_vicp_frame_header,
    encode_vicp_message,
)


def test_vicp_encode_decode_header():
    frame = encode_vicp_message("COMM_HEADER OFF\n", sequence=7)
    frame_header = decode_vicp_frame_header(frame[:VICP_FRAME_HEADER_SIZE])

    assert frame_header["sequence"] == 7
    assert frame_header["payload_len"] == len(b"COMM_HEADER OFF\n")
    assert frame_header["operation"] & OP_DATA
    assert frame_header["operation"] & OP_EOI
    assert frame[VICP_FRAME_HEADER_SIZE:] == b"COMM_HEADER OFF\n"
