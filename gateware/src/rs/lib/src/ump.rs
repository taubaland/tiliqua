//! Universal MIDI Packet (UMP) helpers.
//!
//! [`UmpEndpoint`] answers the UMP Stream messages (message type 0xF) a
//! MIDI 2.0 host sends to discover an endpoint: its name, identity, supported
//! protocols and Function Blocks. Only static, single-Function-Block endpoints
//! (one group, group 0) are supported.

/// UMP message type of UMP Stream messages.
pub const MT_STREAM: u32 = 0xF;

/// Protocol numbers used by Stream Configuration messages.
pub const PROTOCOL_MIDI1: u8 = 0x01;
pub const PROTOCOL_MIDI2: u8 = 0x02;

// UMP Stream message status codes.
const ENDPOINT_DISCOVERY: u16        = 0x000;
const ENDPOINT_INFO: u16             = 0x001;
const DEVICE_IDENTITY: u16           = 0x002;
const ENDPOINT_NAME: u16             = 0x003;
const PRODUCT_INSTANCE_ID: u16       = 0x004;
const STREAM_CONFIG_REQUEST: u16     = 0x005;
const STREAM_CONFIG_NOTIFY: u16      = 0x006;
const FUNCTION_BLOCK_DISCOVERY: u16  = 0x010;
const FUNCTION_BLOCK_INFO: u16       = 0x011;
const FUNCTION_BLOCK_NAME: u16       = 0x012;

// UMP Stream message 'form' (for multi-message text).
const FORM_COMPLETE: u32 = 0;
const FORM_START: u32    = 1;
const FORM_CONTINUE: u32 = 2;
const FORM_END: u32      = 3;

/// Static description of a UMP endpoint.
#[derive(Clone, Copy, Debug)]
pub struct UmpEndpointConfig {
    /// Endpoint name (also used for the single Function Block).
    pub name: &'static str,
    /// Product instance ID (e.g. a serial number).
    pub product_instance_id: &'static str,
    /// `SysEx` manufacturer ID (1-byte IDs are `[id, 0, 0]`).
    pub manufacturer_id: [u8; 3],
    pub family: u16,
    pub model: u16,
    pub version: [u8; 4],
}

/// Answers UMP Endpoint Discovery and Function Block Discovery.
pub struct UmpEndpoint {
    config: UmpEndpointConfig,
    protocol: u8,
}

/// UMP message type of a UMP's first word.
pub fn message_type(word0: u32) -> u32 {
    word0 >> 28
}

fn stream_word0(form: u32, status: u16, data: u16) -> u32 {
    (MT_STREAM << 28) | (form << 26) | (u32::from(status & 0x3FF) << 16) | u32::from(data)
}

/// Send `text` as one or more UMP Stream messages, starting at byte
/// `offset` of each message (after the 2 status bytes and `prefix`).
fn send_text<F: FnMut([u32; 4])>(status: u16, prefix: Option<u8>, text: &str, send: &mut F) {
    let text = text.as_bytes();
    let offset = 2 + usize::from(prefix.is_some());
    let per_msg = 16 - offset;
    let n_msgs = text.len().div_ceil(per_msg).max(1);
    for (n, chunk) in text.chunks(per_msg).chain(
            core::iter::repeat_n(&[][..], usize::from(text.is_empty()))).enumerate() {
        let form = match (n_msgs, n) {
            (1, _) => FORM_COMPLETE,
            (_, 0) => FORM_START,
            (_, n) if n == n_msgs - 1 => FORM_END,
            _ => FORM_CONTINUE,
        };
        let mut bytes = [0u8; 16];
        bytes[..2].copy_from_slice(&stream_word0(form, status, 0).to_be_bytes()[..2]);
        if let Some(p) = prefix {
            bytes[2] = p;
        }
        bytes[offset..offset + chunk.len()].copy_from_slice(chunk);
        let mut msg = [0u32; 4];
        for (w, b) in msg.iter_mut().zip(bytes.chunks(4)) {
            *w = u32::from_be_bytes([b[0], b[1], b[2], b[3]]);
        }
        send(msg);
    }
}

impl UmpEndpoint {
    pub fn new(config: UmpEndpointConfig) -> Self {
        Self { config, protocol: PROTOCOL_MIDI2 }
    }

    /// Protocol most recently selected by the host (Stream Configuration Request).
    pub fn protocol(&self) -> u8 {
        self.protocol
    }

    /// Process one UMP Stream message from the host, calling `send` with
    /// each reply. Other messages (and unknown requests) are ignored.
    pub fn process<F: FnMut([u32; 4])>(&mut self, msg: [u32; 4], mut send: F) {
        if message_type(msg[0]) != MT_STREAM {
            return;
        }
        let status = ((msg[0] >> 16) & 0x3FF) as u16;
        match status {
            ENDPOINT_DISCOVERY => {
                let filter = msg[1] & 0xFF;
                if filter & 0x01 != 0 {
                    // UMP v1.1, 1 static Function Block, MIDI 1.0 + 2.0 protocols, no JR timestamps.
                    send([stream_word0(FORM_COMPLETE, ENDPOINT_INFO, 0x0101),
                          (1 << 31) | (1 << 24) | (1 << 9) | (1 << 8), 0, 0]);
                }
                if filter & 0x02 != 0 {
                    let id = self.config.manufacturer_id;
                    let [fam_lsb, fam_msb] = self.config.family.to_le_bytes();
                    let [mod_lsb, mod_msb] = self.config.model.to_le_bytes();
                    send([stream_word0(FORM_COMPLETE, DEVICE_IDENTITY, 0),
                          u32::from_be_bytes([0, id[0] & 0x7F, id[1] & 0x7F, id[2] & 0x7F]),
                          u32::from_be_bytes([fam_lsb & 0x7F, fam_msb & 0x7F, mod_lsb & 0x7F, mod_msb & 0x7F]),
                          u32::from_be_bytes(self.config.version.map(|v| v & 0x7F))]);
                }
                if filter & 0x04 != 0 {
                    send_text(ENDPOINT_NAME, None, self.config.name, &mut send);
                }
                if filter & 0x08 != 0 {
                    send_text(PRODUCT_INSTANCE_ID, None, self.config.product_instance_id, &mut send);
                }
                if filter & 0x10 != 0 {
                    send(self.stream_config_notify());
                }
            }
            STREAM_CONFIG_REQUEST => {
                let protocol = ((msg[0] >> 8) & 0xFF) as u8;
                if protocol == PROTOCOL_MIDI1 || protocol == PROTOCOL_MIDI2 {
                    self.protocol = protocol;
                }
                send(self.stream_config_notify());
            }
            FUNCTION_BLOCK_DISCOVERY => {
                let block = (msg[0] >> 8) & 0xFF;
                let filter = msg[0] & 0xFF;
                if block != 0 && block != 0xFF {
                    return;
                }
                if filter & 0x01 != 0 {
                    // Block 0: active, UI hint 'receiver', not MIDI 1.0 only, bidirectional.
                    // Spans group 0 only, no MIDI-CI, no SysEx8 streams.
                    send([stream_word0(FORM_COMPLETE, FUNCTION_BLOCK_INFO, 0x8013),
                          0x0001_0000, 0, 0]);
                }
                if filter & 0x02 != 0 {
                    send_text(FUNCTION_BLOCK_NAME, Some(0), self.config.name, &mut send);
                }
            }
            _ => {}
        }
    }

    fn stream_config_notify(&self) -> [u32; 4] {
        [stream_word0(FORM_COMPLETE, STREAM_CONFIG_NOTIFY, u16::from(self.protocol) << 8), 0, 0, 0]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const CONFIG: UmpEndpointConfig = UmpEndpointConfig {
        name: "Tiliqua polysyn",
        product_instance_id: "0000",
        manufacturer_id: [0x7D, 0, 0],
        family: 0,
        model: 0,
        version: [0, 0, 0, 1],
    };

    fn replies(ep: &mut UmpEndpoint, msg: [u32; 4]) -> Vec<[u32; 4]> {
        let mut out = Vec::new();
        ep.process(msg, |r| out.push(r));
        out
    }

    fn text(msgs: &[[u32; 4]], offset: usize) -> String {
        let mut s = Vec::new();
        for m in msgs {
            let bytes: Vec<u8> = m.iter().flat_map(|w| w.to_be_bytes()).collect();
            s.extend(bytes[offset..].iter().filter(|b| **b != 0));
        }
        String::from_utf8(s).unwrap()
    }

    #[test]
    fn endpoint_discovery() {
        let mut ep = UmpEndpoint::new(CONFIG);
        // Endpoint Discovery, UMP v1.1, request everything.
        let r = replies(&mut ep, [0xF000_0101, 0x0000_001F, 0, 0]);
        // info + identity + name (2 messages) + instance id + stream config
        assert_eq!(r.len(), 6);
        assert_eq!(r[0], [0xF001_0101, 0x8100_0300, 0, 0]);
        assert_eq!(r[1], [0xF002_0000, 0x007D_0000, 0, 0x0000_0001]);
        // "Tiliqua polysyn" is 15 bytes: START (14 bytes) + END (1 byte).
        assert_eq!(r[2][0], 0xF403_5469); // form=1, 'T' 'i'
        assert_eq!(r[3][0] >> 16, 0xFC03);  // form=3
        assert_eq!(text(&r[2..4], 2), "Tiliqua polysyn");
        assert_eq!(r[4][0], 0xF004_3030);
        assert_eq!(text(&r[4..5], 2), "0000");
        assert_eq!(r[5], [0xF006_0200, 0, 0, 0]);
    }

    #[test]
    fn endpoint_discovery_filtered() {
        let mut ep = UmpEndpoint::new(CONFIG);
        let r = replies(&mut ep, [0xF000_0101, 0x0000_0004, 0, 0]);
        assert_eq!(r.len(), 2);
        assert_eq!(text(&r, 2), "Tiliqua polysyn");
    }

    #[test]
    fn function_block_discovery() {
        let mut ep = UmpEndpoint::new(CONFIG);
        let r = replies(&mut ep, [0xF010_FF03, 0, 0, 0]);
        assert_eq!(r.len(), 3);
        assert_eq!(r[0], [0xF011_8013, 0x0001_0000, 0, 0]);
        // 13 name bytes per message, after the block number.
        assert_eq!(r[1][0], 0xF412_0054);
        assert_eq!(r[2][0] >> 16, 0xFC12);
        assert_eq!(text(&r[1..], 3), "Tiliqua polysyn");
        // Unknown blocks are ignored.
        assert_eq!(replies(&mut ep, [0xF010_0103, 0, 0, 0]), Vec::<[u32; 4]>::new());
    }

    #[test]
    fn stream_configuration() {
        let mut ep = UmpEndpoint::new(CONFIG);
        assert_eq!(replies(&mut ep, [0xF005_0100, 0, 0, 0]), vec![[0xF006_0100, 0, 0, 0]]);
        assert_eq!(ep.protocol(), PROTOCOL_MIDI1);
        // Unsupported protocol: keep the current one.
        assert_eq!(replies(&mut ep, [0xF005_0700, 0, 0, 0]), vec![[0xF006_0100, 0, 0, 0]]);
        assert_eq!(replies(&mut ep, [0xF005_0200, 0, 0, 0]), vec![[0xF006_0200, 0, 0, 0]]);
    }

    #[test]
    fn ignores_other_messages() {
        let mut ep = UmpEndpoint::new(CONFIG);
        assert_eq!(replies(&mut ep, [0x40B0_1400, 0x8000_0000, 0, 0]), Vec::<[u32; 4]>::new());
    }
}
