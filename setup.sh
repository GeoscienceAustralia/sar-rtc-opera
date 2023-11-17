git clone https://github.com/opera-adt/RTC.git RTC

# build container
cp build_docker_image_otf.sh RTC
cd RTC
sh build_docker_image_otf.sh
cd ..

# make virtual env for downloads
pip install virtualenv
python -m venv rtc_otf_env
source rtc_otf_env/bin/activate
pip install -r requirements.txt

