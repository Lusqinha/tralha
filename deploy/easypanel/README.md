# Easypanel

Deploys tralha on [Easypanel](https://easypanel.io), which builds the image from this repository and handles HTTPS through its own Traefik.

`schema.json` is what you paste into a project's **Templates → Create from Schema**. It creates one app service built from the `master` branch, with the main domain on the private port and a `files-` domain on the public port, a volume at `/data` and all Linux capabilities dropped. `template/` holds the same definition in the layout of [easypanel-io/templates](https://github.com/easypanel-io/templates), ready for a pull request there; its `generate` function with default inputs produces exactly `schema.json`, so change both together.

Traefik reaches the container over Easypanel's internal network, so `TRALHA_FORWARDED_ALLOW_IPS` trusts the private address ranges. The service has no published ports, which keeps those forwarded headers from being spoofed from outside.
