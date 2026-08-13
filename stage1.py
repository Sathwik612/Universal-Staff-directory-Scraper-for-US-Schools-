import sys
from build_texas_staff_directory_links import run
run(askted_csv="tx_sites_live.csv", sample_fraction=1.0,
    output_csv="tx_schools_directories_all.csv", workers=64)