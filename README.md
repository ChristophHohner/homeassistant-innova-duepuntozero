# Innova Duepuntozero – Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration for **Innova Duepuntozero** air conditioning units. It communicates with the Innova cloud API using gRPC and supports real-time state updates via server-sent events.

> **Note:** This integration only supports the Duepuntozero device generation. Older Innova models are not compatible.

## Features

- Current room temperature display
- Target temperature control (16–31 °C, 0.5 °C steps)
- HVAC modes: Auto, Heat, Cool, Fan only, Dry, Off
- Fan speed: Auto, Min, Medium, Max
- Flap (swing) control: On / Off
- Real-time updates via `SubscribeToDeviceEvents` – no need to wait for the next polling cycle
- Configurable fallback polling interval (default: 10 minutes, can be disabled)

## Related projects

> **Older Innova models** (pre-Duepuntozero) expose a local REST API and are **not** supported by this integration.  
> For those devices, use [homeassistant-innova](https://github.com/danielrivard/homeassistant-innova) by @danielrivard instead.

## Requirements

- A Innova account with email/password login  
  *(Google / OAuth sign-in is not yet supported)*
- The MAC address of your Duepuntozero unit  
  *(visible in the Innova app under device settings)*

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → click the three-dot menu → **Custom repositories**
3. Add the URL of this repository and select category **Integration**
4. Search for *Innova Duepuntozero* and install it
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/innova_duepuntozero` folder into your  
   `<config>/custom_components/` directory
2. Restart Home Assistant

## Configuration

After installation, add the integration via:

**Settings → Devices & Services → Add Integration → Innova Duepuntozero**

You will be asked for:
- Your Innova account **email address**
- Your Innova account **password**
- The **MAC address** of your device (format: `AA:BB:CC:DD:EE:FF`)

### Options

After setup, you can adjust the fallback polling interval via  
**Settings → Devices & Services → Innova Duepuntozero → Configure**:

| Option | Default | Description |
|--------|---------|-------------|
| Fallback polling interval | 10 minutes | How often to poll when the event stream is active. Set to `0` to disable polling entirely. |

## Technical background

This integration was developed by reverse-engineering the official Innova Android app. The cloud API uses:
- **HTTPS REST** for authentication
- **gRPC over HTTP/2** for device control and real-time event streaming

The gRPC transport is implemented directly using the `h2` library to avoid native C extensions (`grpcio`) that cannot be compiled in the Home Assistant OS container.

## License

MIT
