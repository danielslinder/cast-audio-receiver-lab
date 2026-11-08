# Experimental Spotify Connect → AirPlay

This optional input uses Spotify’s official Soloist Linux client to decode Spotify audio and the existing AirPlay output to play it on a configured speaker. It creates a separate **Spotify Connect** entry, such as `Kitchen (Spotify)`. Existing Cast entries, route IDs and AirPlay pairings remain unchanged. Spotify Cast authentication is not enabled by this feature.

## Setup

1. Use a personal Spotify Premium account. Generate your own Soloist API key in the [official dashboard](https://developer.spotify.com/dashboard/soloist). Each person needs their own personal key.
2. Open the app’s speaker management page. Expand **Spotify Connect** on an AirPlay speaker, enter its Spotify name and your personal key, enable it, then save. This restarts that speaker’s adapter.
3. Select that name in Spotify and play a track. First-time pairing needs the Spotify sender and receiver on the same local network. Stored pairing is restored on later starts.
4. Leave the password field blank on subsequent saves to retain the key. Replacing the key clears that speaker’s old account session and cache after its worker has stopped; pair the new account again. Disabling retains private configuration for later use.

Independent simultaneous playback requires separate Premium accounts and their respective keys. One Premium account cannot supply two independent streams. This feature does not mirror arbitrary phone audio.

## Runtime and privacy

The image does not contain or redistribute Soloist. Enabling Spotify downloads the official Linux ARM64 or x86_64 archive over HTTPS from `soloist-builds.spotifycdn.com` into private app state. The local SHA-256 provenance record identifies the downloaded archive; it is not an independently published verification hash. The archive is restricted to the official executable, changelog and notices, with no path links or traversal. Spotify’s [download and update rules](https://developer.spotify.com/documentation/soloist/reference/downloads-and-updates) apply. Builds expire after 90 days; startup refreshes builds older than 75 days. Restart the app periodically to receive updates. Download or authentication failures fail that input and leave Cast available.

API keys use mode-0600 files. Configuration responses return only whether a key is set. Native engine output, account data and command arguments are never relayed into app logs. The official CLI receives the key as an argument; processes with sufficient access inside the app container can inspect it. App backups include private Spotify settings and sessions; protect those backups.

Each enabled route has its own engine, cache, private PulseAudio sink and capture process. A single app-private D-Bus and Avahi daemon publish discovery as the application user; no host D-Bus mount or new Home Assistant privilege is required. The control WebSocket binds only to loopback. The runtime uses Debian Trixie to meet the official client’s glibc requirement.

## Audio and controls

PulseAudio produces 44.1 kHz stereo signed 16-bit PCM. A bounded 15-second buffer fails explicitly on overflow. Idle capture is drained; pause, seek and takeover discard queued audio. Spotify controls own pause/resume, seek and track navigation. Track metadata and artwork use the existing AirPlay metadata path. Spotify’s volume adjusts its private audio stream; the AirPlay receiver volume remains the output level, so both influence audible volume. Hardware volume feedback into Spotify’s slider is not currently synchronized.

Cast and Spotify share exclusive ownership of each physical output. A new Cast load pauses Spotify and detaches its capture. Delayed Spotify state updates cannot reclaim the speaker until a new playback transition. Other speakers continue independently. Native failures have at most three startup attempts with bounded cleanup; save settings to retry after inspecting the state.

## Acceptance status

On Home Assistant OS with an ARM64 Raspberry Pi and **0.7.0-dev3**, the household tester confirmed audible Spotify music on both configured HomePods. Pause/resume, seeking and Next also passed on the first speaker. Existing route identities, personal keys and paired sessions survived the update. Native AMD64 and ARM64 image smoke tests and the automated regression suite passed; this does not certify other speaker models.

Version dev3 corrects playback-position handling: Soloist reports a position anchor with a timestamp and playback speed. Repeated anchors must be extrapolated instead of being mistaken for seeks during AirPlay startup. Intentional cancellation of a buffering load also detaches its old PCM source before cleanup can report an error.

A continuous 30-minute installed playback run, explicit disconnect/reconnect, simultaneous independent sessions with two accounts, and audible Cast regression after the update remain unverified. Discovery, a running process, or the earlier development probe do not satisfy these checks. The feature remains experimental while that acceptance work is open.

References: [authentication](https://developer.spotify.com/documentation/soloist/concepts/authentication), [CLI](https://developer.spotify.com/documentation/soloist/reference/command-line), [control API](https://developer.spotify.com/documentation/soloist/reference/websocket-api).
