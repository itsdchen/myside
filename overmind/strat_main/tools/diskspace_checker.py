#! /usr/bin/env python
import argparse


"""

To run, I scp'd this and strat_main/util/email.py to the /tools dir of the remote machine.
And I added this to the crontab:
00 15 * * * ;  ./tools/diskspace_checker.py --machinename scapbfut

"""

import os
import smtplib
import logging

# Since this may need to live in its own scp'd dir, I'll just
# push some of these util libraries over manually too.

from util import email

# Set up logging
logging.basicConfig(filename="disk_space_monitor.log", level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

def check_disk_space(path):
    """Checks the free disk space on the specified path.
    Returns:
        The free disk space in megabytes.
    """
    try:
        stats = os.statvfs(path)
        free_space_mb = (stats.f_bavail * stats.f_frsize) / (1024 * 1024)  # Convert to MB
        logging.info(f"Free space in {path}: {free_space_mb:.2f} MB")
        return free_space_mb
    except OSError as e:
        logging.error(f"Error getting disk space: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(
        description="Monitor disk space and send email alerts when space is low",
    )
    parser.add_argument("--machinename", required=True)
    args = parser.parse_args()

    path = "/"  # Check root directory
    threshold_mb = 10  # 10MB threshold

    free_space_mb = check_disk_space(path)
    if free_space_mb is not None and free_space_mb < threshold_mb:
        subject = f"Low Disk Space Alert on {args.machinename}"
        message = f"Free disk space on {args.machinename} is below {threshold_mb}MB: {free_space_mb:.2f} MB"
        logging.warning(message)
        email.send_mail(
            subject=subject,
            body=message,
        )
    else:
        logging.info("Disk space check completed - space is sufficient")


if __name__ == "__main__":
    main()
