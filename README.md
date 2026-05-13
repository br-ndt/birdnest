<img src="logo.png" width="128" height="128" style="border-radius: 100%;">

# birdnest

An birdcam aggregator service. Runs anywhere you can run python, javascript, and bash.

## Setup

### Setup birdcams

See [birdcam README](https://github.com/br-ndt/birdcam), then

```bash
cp birdnest.toml.example birdnest.toml
```

and update `birdnest.toml` with correct birdcam hostnames and desired nicknames.

### Install python dependencies
```bash
sudo apt install -y python3-flask python3-requests
```

### Test manually

```bash
BIRDCAM_TOKEN=abc123 python3 birdnest.py
```

Visit `http://{hostname}:8000/api/cams` and you should see your connected birdcams in JSON. `BIRDCAM_TOKEN` should match the token given to ALL birdcams (per birdcam auth on the roadmap).


## Systemd Service

If you want this to persist:

```bash
cp birdnest.service /etc/systemd/system/birdnest.service
sudo mkdir /etc/birdnest && cp env /etc/birdnest/env
sudo systemctl daemon-reload
sudo systemctl enable birdnest.service
sudo systemctl start birdnest.service
sudo systemctl status birdnest.service
journalctl -fu birdnest.service # follow logs
```

## Frontend

Take a look in the `frontend` dir if you want a view for these.

Happy birdnesting!
