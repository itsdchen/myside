#!/bin/bash

#
# Usage: ./run-job.sh "Job Name" "job.py" "arg1" "arg2" ...
#
# Runs an executable with some standardized handling
# 1. A full job name is constructed from arguments
# 2. stdout/err are logged to standardized filenames based on full job name
# 3. a standardized email based on full job name is sent on executable error
#

# Function to display usage
usage() {
    echo "Usage: $0 job_name executable [arguments...]"
}

# Check minimum number of arguments
if [ "$#" -lt 2 ]; then
    echo "Error: Too few arguments."
    usage
    exit 1
fi

# Replace spaces with underscores in job name
jobname="${1// /_}"
executable=$2

# Verify that the executable exists
if [ ! -f "$executable" ]; then
    echo "Error: Executable $executable not found."
    exit 1
fi

# Parse args
shift 2
args=("$@")

# Join arguments with '_' for constructing full job name
joined=""
for arg in "${args[@]}"; do
    # Check if joined is empty to avoid adding an underscore at the beginning
    if [ -z "$joined" ]; then
        joined=$arg
    else
        joined=${joined}_$arg
    fi
done
jobnamefull=${jobname}_$joined

outfile=$HOME/logs/$jobnamefull.stdout
errfile=$HOME/logs/$jobnamefull.stderr

echo "Running: $executable ${args[@]}"

ulimit -c unlimited
# Run executable, logging output to file
$executable "${args[@]}" > $outfile 2> $errfile
exitcode=$?

echo "$(date) exited with exitcode: $exitcode" >> $errfile

# Executable errored, so send failure email
if [ $exitcode -ne 0 ]; then
    echo "Sending failure email"
    # Construct email parameters
    recipients="itsdchen@gmail.com"
    subject="[$(date)] $jobnamefull Failed"
    mail -s "$subject" -a "From: PK Monitor <itsdchen@gmail.com>" $recipients <<EOF
machine: $(hostname)
stdout: $outfile
stderr: $errfile
EOF
fi

exit $exitcode
