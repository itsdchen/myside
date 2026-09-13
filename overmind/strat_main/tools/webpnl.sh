#!/bin/bash

while true; do # loop the whole thing every 10s (suppress indent)

echo "Running at: $(date)"

# Get day of start of trading
current_hour_et=$(TZ='America/New_York' date +%H)
if [ "$current_hour_et" -lt 18 ]; then
    pos_day=$(TZ='America/New_York' date -d 'yesterday' +%Y%m%d)
    trade_day=$(TZ='America/New_York' date +%Y%m%d)
else
    pos_day=$(TZ='America/New_York' date +%Y%m%d)
    trade_day=$(TZ='America/New_York' date -d 'tomorrow' +%Y%m%d)
fi

# Setup for whether running on old, l1g, or vault
local_file="$HOME/scratch/start_pos/pos_$pos_day.log"
local_file_preipo="$HOME/scratch/start_pos/preipo/pos_$pos_day.log"
remote_file="~/scratch/start_pos/pos_$pos_day.log"
remote="ace"
web_dir="$HOME/webpnl"
trades_file="$HOME/logs/trades/trades_$trade_day.csv"
trades_file_preipo="$HOME/logs/trades/preipo/trades_$trade_day.csv"
touch "$trades_file_preipo"
# Periodic re-mark snapshots written by hl_trade_sub.py (0-size MARK rows). Used
# only for the time-series graphs, not the table. Touch so it always exists.
marks_file="$HOME/logs/trades/marks_$trade_day.csv"
marks_file_preipo="$HOME/logs/trades/preipo/marks_$trade_day.csv"
touch "$marks_file_preipo"

# Get positions file if necessary
if ! [[ -s "$local_file" ]]; then
    if [[ "$HOSTNAME" = "$remote" ]]; then
        echo "Local pos file not found. Already on $remote, nothing to download."
        exit 1
    else
        echo "Local pos file not found. Downloading from $remote."
        ssh "$remote" "cat $remote_file" > "$local_file"
    fi
fi
if ! [[ -s "$local_file_preipo" ]]; then
    touch "$local_file_preipo"
    # cat "$local_file" | egrep "xyz:SPCX" > "$local_file_preipo"
fi

# Generate showpnl table
echo "Using local pos file $local_file."
~/.venvs/v1/bin/python ~/bin/pnl.py "$trades_file" "$trades_file_preipo" --start-pos "$local_file" \
                       --mark-to-current --fee-from-csv > "$web_dir/showpnl.txt"
~/.venvs/v1/bin/python ~/bin/pnl.py "$trades_file_preipo" --start-pos "$local_file_preipo" \
                       --mark-to-current --fee-from-csv > "$web_dir/preipo/showpnl.txt"

# Generate the TOTAL pnl graph
last_modified=$(date -r "$web_dir/fig_total.png" +%s)
now=$(date +%s)
elapsed=$((now - last_modified))
if (( elapsed >= 60 )); then
    echo "Generating fig_total.png."
    ~/.venvs/v1/bin/python ~/bin/pnl.py "$trades_file" "$trades_file_preipo" \
                           "$marks_file" "$marks_file_preipo" \
                           --start-pos "$local_file" --mark-to-current  --fee-from-csv \
                           --time-series > "$web_dir/timeseries.csv"
    cat "$web_dir/timeseries.csv" | grep TOTAL | ~/bin/plot.py --out-file "$web_dir/fig_total.png"
    # cat "$web_dir/timeseries.csv" | egrep "xyz:SPCX" | ~/bin/plot.py --out-file "$web_dir/preipo/fig_total.png"
else
    echo "Skipping fig_total.png."
fi

# Split trades file into useq, comm, asia, other
# Generate each category's showpnl table and TOTAL pnl graph
cat "$trades_file" "$trades_file_preipo" "$marks_file" "$marks_file_preipo" \
    | ~/bin/split_trades.py -o "$web_dir"
~/bin/split_trades.py -c | parallel -j 8 \
    "~/.venvs/v1/bin/python ~/bin/pnl.py $web_dir/trades.{}.csv --start-pos $local_file --mark-to-current --fee-from-csv --categ {} > $web_dir/showpnl.{}.txt"

# Generate ADD/REM showpnl tables: the overall trades file split by tag (not per
# category), so no --categ symbol filter and no --start-pos opening inventory.
for tag in add rem; do
    ~/.venvs/v1/bin/python ~/bin/pnl.py "$web_dir/trades.$tag.csv" \
                           --mark-to-current --fee-from-csv > "$web_dir/showpnl.$tag.txt"
done

# Generate each category's TOTAL pnl graph
last_modified=$(date -r "$web_dir/fig_useq.png" +%s)
elapsed=$((now - last_modified))
if (( elapsed >= 60 )); then
    echo "Generating category plots."
    ~/bin/split_trades.py -c | parallel -j 8 \
        "~/.venvs/v1/bin/python ~/bin/pnl.py $web_dir/trades.{}.csv --start-pos $local_file --mark-to-current --fee-from-csv --categ {} --time-series | grep TOTAL | ~/bin/plot.py --out-file $web_dir/fig_{}.png"
    for tag in add rem; do
        ~/.venvs/v1/bin/python ~/bin/pnl.py "$web_dir/trades.$tag.csv" \
                               --mark-to-current --fee-from-csv --time-series \
            | grep TOTAL | ~/bin/plot.py --out-file "$web_dir/fig_$tag.png"
    done
else
    echo "Skipping category plots."
fi

# Generate the individual symbol pnl graphs
last_modified=$(date -r "$web_dir/symbols.txt" +%s)
elapsed=$((now - last_modified))
if (( elapsed >= 300 )); then
    echo "Generating symbol plots."
    mkdir -p "$web_dir/figs"
    cat "$trades_file" "$trades_file_preipo" | awk -F, '{print $4}' | sort | uniq > "$web_dir/symbols.txt"
    cat "$web_dir/symbols.txt" | parallel -j 8 \
    "grep {} $web_dir/timeseries.csv | ~/bin/plot.py --out-file $web_dir/figs/fig_{}.png"
else
    echo "Skipping symbol plots."
fi

# Rewrite the leading symbol of each pnl table row into a link to that symbol's
# figure. Rows with no matching figure (header, separators, TOTAL) pass through
# unchanged. Widths are preserved since the link text is the symbol itself.
linkify_syms() {
    ls "$web_dir/figs" 2>/dev/null | awk '
        NR==FNR { sym=$0; sub(/^fig_/, "", sym); sub(/\.png$/, "", sym); have[sym]=1; next }
        have[$1] { sub(/^[^ ]+/, "<a href=\"figs/fig_" $1 ".png\">" $1 "</a>") }
        { print }' - "$1"
}

# Build the main HTML file
echo "Generating index.html."
# Linkified table, inlined below and refetched by the page's auto-refresh script.
linkify_syms "$web_dir/showpnl.txt" > "$web_dir/showpnl.html"
cat <<EOF > "$web_dir/index.html"
<!DOCTYPE html>
<html>
<head>
    <title>PnL Dashboard</title>
    <style>
        body { font-family: sans-serif; padding: 20px; background: #f4f4f4; }
        .nav-link {
            display: inline-block;
            margin-bottom: 20px;
            padding: 10px 15px;
            background: #007bff;
            color: white;
            text-decoration: none;
            border-radius: 5px;
        }
        .nav-link:hover { background: #0056b3; }
        pre { background: #eee; padding: 15px; border-radius: 5px; }
        /* Symbol links in the table look exactly like the surrounding text. */
        pre a { color: inherit; text-decoration: none; }
        img { max-width: 100%; height: auto; border: 1px solid #ccc; }
    </style>
</head>
<body>
    <a href="symbols.html" class="nav-link">Symbol Plots</a>
    <a href="useq.html" class="nav-link">USEQ</a>
    <a href="etf.html" class="nav-link">ETF</a>
    <a href="comm.html" class="nav-link">COMM</a>
    <a href="asia.html" class="nav-link">ASIA</a>
    <a href="other.html" class="nav-link">OTHER</a>
    <a href="new.html" class="nav-link">NEW</a>
    <a href="add.html" class="nav-link">ADD</a>
    <a href="rem.html" class="nav-link">REM</a>
<!--    <a href="preipo/index.html" class="nav-link">PREIPO</a> -->

    <h1>PnL Dashboard</h1>
    <p id="timestamp"></p>

    <pre id="pnl-table">$(cat $web_dir/showpnl.html)</pre>

    <img id="pnl-plot" src="fig_total.png?t=$(date +%s)" alt="PnL Plot">

    <script>
        document.getElementById('timestamp').textContent = 'Last updated: ' + new Date().toLocaleString();
        setInterval(function() {
            fetch('showpnl.html?t=' + Date.now())
                .then(function(r) { return r.text(); })
                .then(function(txt) {
                    document.getElementById('pnl-table').innerHTML = txt;
                    document.getElementById('timestamp').textContent = 'Last updated: ' + new Date().toLocaleString();
                });
        }, 10000);

        setInterval(function() {
            document.getElementById('pnl-plot').src = 'fig_total.png?t=' + Date.now();
        }, 60000);
    </script>
</body>
</html>
EOF

# cat <<EOF > "$web_dir/preipo/index.html"
# <!DOCTYPE html>
# <html>
# <head>
#     <title>PnL PreIPO</title>
#     <style>
#         body { font-family: sans-serif; padding: 20px; background: #f4f4f4; }
#         .nav-link {
#             display: inline-block;
#             margin-bottom: 20px;
#             padding: 10px 15px;
#             background: #007bff;
#             color: white;
#             text-decoration: none;
#             border-radius: 5px;
#         }
#         .nav-link:hover { background: #0056b3; }
#         pre { background: #eee; padding: 15px; border-radius: 5px; }
#         img { max-width: 100%; height: auto; border: 1px solid #ccc; }
#     </style>
# </head>
# <body>
#     <a href="../index.html" style="color: #4da3ff; text-decoration: none;">&larr; Back to Dashboard</a>
#     <br>
#     <a href="../symbols.html" class="nav-link">Symbol Plots</a>
#     <a href="../useq.html" class="nav-link">USEQ</a>
#     <a href="../etf.html" class="nav-link">ETF</a>
#     <a href="../comm.html" class="nav-link">COMM</a>
#     <a href="../asia.html" class="nav-link">ASIA</a>
#     <a href="../other.html" class="nav-link">OTHER</a>
#     <a href="../new.html" class="nav-link">NEW</a>
#     <a href="index.html" class="nav-link">PREIPO</a>
#     <h1>PnL PreIPO</h1>
#     <p id="timestamp"></p>

#     <pre id="pnl-table">$(cat $web_dir/preipo/showpnl.txt)</pre>

#     <img id="pnl-plot" src="fig_total.png?t=$(date +%s)" alt="PnL Plot">

#     <script>
#         document.getElementById('timestamp').textContent = 'Last updated: ' + new Date().toLocaleString();
#         setInterval(function() {
#             fetch('showpnl.txt?t=' + Date.now())
#                 .then(function(r) { return r.text(); })
#                 .then(function(txt) {
#                     document.getElementById('pnl-table').textContent = txt;
#                     document.getElementById('timestamp').textContent = 'Last updated: ' + new Date().toLocaleString();
#                 });
#         }, 10000);

#         setInterval(function() {
#             document.getElementById('pnl-plot').src = 'fig_total.png?t=' + Date.now();
#         }, 60000);
#     </script>
# </body>
# </html>
# EOF

# Build each category's HTML file
echo "Generating category html files."
mapfile -t categs <<< $(~/bin/split_trades.py -c)
categs+=("add" "rem")  # ADD/REM tag pages, built from the same template
for categ in "${categs[@]}"; do
    cat <<EOF > "$web_dir/$categ.html"
<!DOCTYPE html>
<html>
<head>
    <title>PnL ${categ^^}</title>
    <meta http-equiv="refresh" content="60">
    <style>
        body { font-family: sans-serif; padding: 20px; background: #f4f4f4; }
        .nav-link { 
            display: inline-block; 
            margin-bottom: 20px; 
            padding: 10px 15px; 
            background: #007bff; 
            color: white; 
            text-decoration: none; 
            border-radius: 5px;
        }
        .nav-link:hover { background: #0056b3; }
        pre { background: #eee; padding: 15px; border-radius: 5px; }
        /* Symbol links in the table look exactly like the surrounding text. */
        pre a { color: inherit; text-decoration: none; }
        img { max-width: 100%; height: auto; border: 1px solid #ccc; }
    </style>
</head>
<body>
    <a href="index.html" style="color: #4da3ff; text-decoration: none;">&larr; Back to Dashboard</a>
    <br>
    <a href="symbols.html" class="nav-link">Symbol Plots</a>
    <a href="useq.html" class="nav-link">USEQ</a>
    <a href="etf.html" class="nav-link">ETF</a>
    <a href="comm.html" class="nav-link">COMM</a>
    <a href="asia.html" class="nav-link">ASIA</a>
    <a href="other.html" class="nav-link">OTHER</a>
    <a href="new.html" class="nav-link">NEW</a>
    <a href="add.html" class="nav-link">ADD</a>
    <a href="rem.html" class="nav-link">REM</a>
<!--    <a href="preipo/index.html" class="nav-link">PREIPO</a> -->

    <h1>PnL ${categ^^}</h1>
    <p>Last updated: $(date)</p>
    
    <pre>$(linkify_syms "$web_dir/showpnl.$categ.txt")</pre>

    <img src="fig_$categ.png?t=$(date +%s)" alt="PnL Plot">
</body>
</html>
EOF
done

# Build the 2nd HTML file with symbol plots
echo "Generating symbol html file."
SYMS_PAGE="$web_dir/symbols.html"
cat <<EOF > $SYMS_PAGE
<!DOCTYPE html>
<html>
<head>
    <title>Symbol PnL</title>
    <style>
        body { font-family: sans-serif; background: #222; color: #fff; padding: 20px; }
        .grid-container {
            display: grid;
            grid-template-columns: repeat(3, 1fr); /* 3 columns across */
            gap: 5px;
        }
        .grid-item {
            background: #333;
            padding: 5px;
            border-radius: 4px;
            text-align: center;
        }
        img {
            width: 100%; /* Scales image to fit the 3-column width */
            height: auto;
            display: block;
            border-radius: 2px;
        }
        .label { font-size: 12px; margin-top: 5px; color: #aaa; }
    </style>
</head>
<body>
    <a href="index.html" style="color: #4da3ff; text-decoration: none;">&larr; Back to Dashboard</a>
    <h1>Symbol Plots</h1>
    <p>Last updated: $(date)</p>
    <div class="grid-container">
EOF

# Loop through all figs and add them to the grid
shopt -s nullglob
for img in $web_dir/figs/*.png; do
    img_name=$(basename "$img")
    echo "      <div class='grid-item'>" >> $SYMS_PAGE
    echo "        <a href='figs/$img_name'>" >> $SYMS_PAGE
    echo "          <img src='figs/$img_name?t=$(date +%s)' alt='$img_name'>" >> $SYMS_PAGE
    echo "        </a>" >> $SYMS_PAGE
    echo "        <div class='label'>$img_name</div>" >> $SYMS_PAGE
    echo "      </div>" >> $SYMS_PAGE
done

# Close the tags
cat <<EOF >> $SYMS_PAGE
    </div>
</body>
</html>
EOF

echo "Sleeping."
sleep 10
done # end outer loop
