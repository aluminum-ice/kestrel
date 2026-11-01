# Census protocol: `kestrel-census/1`

The `census` plugin keeps an anonymous count of active Kestrels in the public
BitTorrent DHT, with no server. This page is the format: what a device writes,
where, and how a reader counts it, so the plugin, the desktop report
(`tools/census-report/`) and any other reader agree. The code is the plugin's
helper files, `builder/extra/files/plugins/_census_dht.py` (the DHT client) and
`_census_proto.py` (this format).

A change to anything here is a new protocol name (`kestrel-census/2`), with new
slot keys, so old and new devices never mix entries in one slot.

## Slots

Each UTC day has **64 slots**, numbered 0-63. Each slot is a
[BEP 44](https://www.bittorrent.org/beps/bep_0044.html) mutable item whose
signing key is derived from a public string:

```text
kestrel-census/1|YYYY-MM-DD|<slot>          the slot
kestrel-census/1|YYYY-MM-DD|<slot>|a<N>     its alternate N, 1-3
```

- **Private key:** the Ed25519 key whose 32-byte seed is the SHA-256 of that
  string (UTF-8).
- **Public key:** its raw 32 bytes.
- **Target:** the SHA-1 of the public key (BEP 44, no salt).

So every device, and anyone else, can read and write every slot. The date is
the UTC day of the write.

## The value

A slot's value is a run of **20-byte entries**, one per device that wrote in
the current hour or the one before, sorted by token. At most **49** entries
(980 bytes) fit in one BEP 44 value (1000 bytes). An entry, in network byte
order:

```text
token(8) | hour(1) | version(2) | agent(1) | contests(4) | pow_nonce(4)
```

| Field | Meaning |
| --- | --- |
| `token` | 8 random bytes, made at the device's first write of a UTC day and kept all day (across reboots). A new day gets a new token, so days aren't linked. |
| `hour` | The UTC hour of the device's last write, 0-23. |
| `version` | The release, 16 bits: `major(3) \| minor(4) \| patch(6) \| beta(3)`. `beta` is 0 for a stable release, 1-6 for `-beta.1` to `-beta.6`, 7 for `-beta.7` or later. All zeros is "unknown": a development build, an unreadable version file, or a part out of range (major > 7, minor > 15, patch > 63). |
| `agent` | 8 bits: `state(2) \| algorithm(3) \| remote(3)`, below. |
| `contests` | 4 bytes: on-device won, on-device lost, remote won, remote lost, since UTC midnight, each saturating at 255. |
| `pow_nonce` | 4 bytes, below. |

**`agent` byte:**

- `state`: 0 no agent status, 1 the agent isn't running, 2 scripted sweep
  (`policy = baseline`), 3 a learned policy.
- `algorithm`: 0 unknown, 1 RecurrentPPO, 2 PPO, 3 DQN, 4 QR-DQN, 5-7 reserved.
  The configured algorithm, whatever the state.
- `remote`, from the `remote-train` plugin's `remote-training` service, worked
  out once per UTC day at the day's first write:

| Value | Meaning | From the service |
| --- | --- | --- |
| 0 | not enabled | the service is absent |
| 1 | enabled, not configured | `configured` false |
| 2 | configured, failing | `stack` is `failed`, or `last_contact` is missing or older than 24 h |
| 3 | configured, no new model | otherwise, `last_model` missing or older than 7 days |
| 4 | working | `last_model` within 7 days |
| 5-7 | reserved | |

**Contests** come from the agent status's `promotion_tally` (`local` and
`remote`, `won` and `lost`), minus a baseline: the tally's last reading before
UTC midnight, or its first reading of the day. A tally that goes down (the
agent's state was reset) becomes the new baseline. Adoption counts from before
contests existed aren't counted.

**Proof-of-work:** an entry counts only when the SHA-256 of

```text
"kc1|" + YYYY-MM-DD + token + pow_nonce
```

starts with **20 zero bits**. It binds the token to the day, and makes each
token cost about a million hashes (a second or two on a Raspberry Pi 5). A
device finds it once a day, before its first write.

## Writing

A device writes once per UTC hour, at a random minute, and never in its first
5-30 minutes online:

1. **Slot:** one random slot for the day, kept all day.
2. **Read** the slot from the nodes that store it: the ones cached earlier in
   the day if at least 4 still answer, otherwise a lookup toward its target.
3. **Merge:** the union of every copy (the newest entry per token, entries with
   a bad hour or proof-of-work dropped), keeping only entries from this hour
   and the one before, with the device's own entry in place of its older one.
4. **Put** the merged value with `seq` = minutes since UTC midnight × 100 + a
   random 0-99, above the highest `seq` seen. A node that answers BEP 44 error
   302 (a newer `seq`) sends the writer back to step 2, once.
5. **Check,** 20-60 seconds later: read the slot again. If no copy holds the
   device's entry (another writer's put landed over it), write once more.

A slot with more than 49 entries after the merge, or whose stored `seq` is
above **1,000,000** (no honest writer gets there), sends the writer to the
slot's next alternate, and it stays there for the day. A slot no node answers
for is skipped the same way.

A writer keeps every other device's entry from this hour and the one before.

## Reading

A reader reads all 64 slots of a day. It reads a slot's alternates in order
only while the slot it just read is full (49 entries) or past the `seq`
ceiling. It keeps the newest entry per token across every copy and alternate,
and drops entries with an hour above 23 or a failed proof-of-work.

A slot holds at most 49 entries, so a reader keeps at most 49 from one slot's
copies: when the copies together hold more, it keeps the entries held by the
most copies (then the later hour). Over a day it keeps at most a full census,
64 slots x 4 locations x 49 = 12,544 entries; past that, entries it already has
still update, and new ones are only tallied as "not counted" (the Census
page then says the census is full).

- **Online:** entries whose hour is the reader's current UTC hour or the one
  before.
- **Active today:** every token seen in the day's reads (the plugin adds up its
  reads through the day, since an entry leaves its slot about two hours after
  its device's last write).
- **Contests:** sums over entries. An entry claiming more than 100 on-device
  or 50 remote contests in a day has its contests left out: the agent runs at
  most one on-device contest every half hour or so, and one per remote model.
- Tokens are new every day, so days can't be added up into a device count.

The numbers are approximate and unverified, and count only devices that run
the census.

## DHT behaviour

- Every query carries `ro = 1` (BEP 43, read-only: don't add this node to
  routing tables) and no client version (`v`). The client answers no queries.
- A lookup toward a census slot's target queries **only nodes whose ID matches
  their IP address** under [BEP 42](https://www.bittorrent.org/beps/bep_0042.html).
  Node-list upkeep (a `find_node` toward a random target) may use any node.
- The client keeps a node list of about 300 public IPv4 nodes, joins through
  the bootstrap routers only when that list has gone stale, and is limited to
  10 queries a second and 8 in flight.
- IPv4 only.
