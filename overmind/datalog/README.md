

# Setup

## Capture box
```bash
# sync scripts
# Replace the ssh instruction to the appropriate cpature box.
rsync -avz -e "ssh -i dkamm_pair.pem" overmind/datalog/ ubuntu@ec2-3-112-229-165.ap-northeast-1.compute.amazonaws.com:/home/ubuntu

# setup
./setup.sh

# install crontab
crontab capture.crontab

# set timezone and restart cron daemon
sudo timedatectl set-timezone US/Eastern
sudo service cron restart

```

## Postprocessing box
```bash

# sync scripts to postprocessing box
rsync -avz -e "ssh -i dkamm_pair.pem" overmind/datalog/ ubuntu@ec2-35-72-159-22.ap-northeast-1.compute.amazonaws.com:/home/ubuntu

# setup
./setup.sh

# install crontab
crontab postprocessing.crontab

# set timezone and restart cron daemon
sudo timedatectl set-timezone US/Eastern
sudo service cron restart

```

# Folder structure

```bash
# cron scripts
/home/ubuntu/cron

# python scripts
/home/ubuntu/tools

# raw captures
/home/ubuntu/raw/$market/$date.{capture,snapshot}

# split captures
/home/ubuntu/split/$market/$sym_$stream_$date.jsonl

# compressed and converted captures
/home/ubuntu/gzpbf/$market/$sym_$stream_$date.gzpbf
```

# S3 Structure
The s3 structure mirrors the folder structure. We do not store the split jsonl in s3 since they are just an intermediate step.

```bash
# raw captures
s3://pktrade-capture/raw/$market/$date.{capture,snapshot}

# compressed and converted captures
s3://pktrade-capture/gzpbf/$market/$sym_$stream_$date.gzpbf
```

# Updating for HyperliquidNodeV1

The HL node feed is captured separately from HL WS — see
`cron/daily-capture-hlnode.sh` and `cron/postprocess-captures-hlnode.sh`.
Market name in S3 is `HyperliquidNodeV1`.

When deploying to an existing capture or postproc box, the standard
`rsync` step at the top of this README picks up the new scripts and
the updated crontab files. A few extras are needed because the HL node
flow uses two new binaries:

1. **Capture box** (must be inside the same VPC as n1, i.e. reach
   `tcp://172.31.37.166:5555/5556`):

   ```bash
   # rsync as documented above, then:
   # copy the up-to-date simple_datalog into ./tools/ (must include
   # --hlnode / --node-endpoint flags — built from this tree).
   scp -i <pem> bin/simple_datalog <box>:/home/ubuntu/tools/

   # install crontab (now includes the two HLNode entries)
   crontab capture.crontab
   ```

2. **Postproc box**:

   ```bash
   # rsync as documented, then:
   # copy hlnode_gzsplitter into ./tools/ (new binary added 2026-06).
   scp -i <pem> bin/hlnode_gzsplitter <box>:/home/ubuntu/tools/

   # install crontab (now includes the HyperliquidNodeV1 entry)
   crontab postprocessing.crontab
   ```

3. **Sanity check** (capture box, dry run):

   ```bash
   # Run a short capture against books only and confirm it produces a
   # non-empty pb.gz.
   ./tools/simple_datalog --market Hyperliquid --hlnode \
     --node-endpoint tcp://172.31.37.166:5555 \
     --date 20300101 --outpath /tmp/hlnode_test.pb.gz &
   sleep 10
   kill -INT $!
   ls -lh /tmp/hlnode_test.pb.gz   # should be tens of MB
   ```

   Postproc box:

   ```bash
   # Confirm splitter runs on the test file and writes a per-(sym, stream)
   # .gzpbf under /tmp.
   mkdir -p /tmp/hlnode_test_gzpbf
   ./tools/hlnode_gzsplitter --src /tmp/hlnode_test.pb.gz \
     --outdir /tmp/hlnode_test_gzpbf --date 20300101
   ls /tmp/hlnode_test_gzpbf
   ```

If those work, the cron entries will pick up the next 23:58 ET roll
naturally.