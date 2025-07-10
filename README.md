# opera-RTC-otf
On the fly production of sentinel-1 backscatter using the opera_adt/RTC workflow - https://github.com/opera-adt/RTC

#### ⚠️ Warning: This project currently uses a fork of tag v1.0.4 of the main repository with version isce3==0.24.4. The fork and branch can be found here and is referenced in the project Dockerfile - https://github.com/abradley60/RTC/tree/software_updates

# Requirments
- Git
- Docker
- Conda (mamba best for speed)
- The following setup scripts expects fedora linux instance (e.g. aws linux ami - packages installed with yum)

# Setup
**1. Add user credentials to the example files stored in the credentials folder:**
- Earthdata credentials - https://urs.earthdata.nasa.gov/users/new
    - Add these to both *credentials_earthdata.yaml* file
- Copernicus Dataspace - https://dataspace.copernicus.eu/
    - Add these to *credentials_copernicus.yaml*
- AWS credentials 
    - Add these to *credentials_aws.yaml* to enable DEM download and upoad to desired destination 

**2. run install script**
- if conda is not installed on instance (mamba will be installed)
```bash
sh setup.sh --install-conda
```
- if running with suitable conda install
```bash
sh setup.sh 
```

# Instructions
1. Set scene and data path details in config.yaml. This is the overarching config file to specify the scenes to process and paths.
2. Activate the environment.
```bash
conda activate rtc_opera
```
3. Run the script
```bash
python rtc_otf.py -c config.yaml

# or use nohop so connection is not lost on EC2

nohup python rtc_otf.py -c config.yaml
```

- In a new terminal, run the following to trace the output from the nohup file
```bash
tail -f nohup.out
```