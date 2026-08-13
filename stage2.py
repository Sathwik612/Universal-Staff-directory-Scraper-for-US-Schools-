import sys
for m in ("build_texas_staff_directory_links","master_staff_directory_scraper"): sys.modules.pop(m,None)
from build_texas_staff_directory_links import run_leads
run_leads("tx_schools_directories_all.csv", output_csv="tx_schools_leads.csv",
          verify_ssl=False, workers=32, follow_profiles=True)