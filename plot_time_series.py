import sys
import time

from ConfigParser import ConfigParser
from ModisExtent import ModisAvailableCountry
from ModisExtent import ModisExtent
import cairo
import cgi
from gdalconst import GA_ReadOnly
from geoalchemy import WKTSpatialElement
from geoalchemy import functions as spfunc
import logging
import logging.config
import os
import osgeo.gdal as gdal
import osgeo.osr as osr
from pyspatialite import dbapi2 as spatialite
import rpy2.rinterface as rinterface
import rpy2.robjects as robjects
from rpy2.robjects.packages import importr
try:
    import simplejson as json
except ImportError:
    import json
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.expression import or_
from tempfile import NamedTemporaryFile
try:
    from cStringIO import StringIO
except ImportError:
    import StringIO
from random import random

logging.config.fileConfig('logging.ini')
# Get the root logger from the config file
log = logging.getLogger(__name__)
# Get also the spatial logger, which logs the requested to a separate csv file
spatiallog = logging.getLogger("spatial.logger")

# ZOO Constants
# See also http://zoo-project.org/docs/workshop/2012/first_service.html
SERVICE_FAILED = 4
SERVICE_SUCCEEDED = 3

# Send all output especially the R output to the dump
#f = open(os.devnull, 'w')
#sys.stdout = f

def ExtractTimeSeries(conf, inputs, outputs):

    try:
        lon = float(inputs['lon']['value'])
        lat = float(inputs['lat']['value'])
    except ValueError:
        conf["lenv"]["message"] = "Parameter \"lon\" or \"lat\" is not valid."
        return SERVICE_FAILED

    try:
        epsg = int(inputs['epsg']['value'])
    except ValueError:
        conf["lenv"]["message"] = "Parameter \"epsg\" is not valid or not supported."
        return SERVICE_FAILED
    if epsg not in [4326]:
        conf["lenv"]["message"] = "Request CRS is not supported."
        return SERVICE_FAILED

    handler = ModisTimeSeriesHandler(conf, inputs, outputs)
    result = handler.extract_values((lon, lat), epsg)

    return result

class ModisTimeSeriesHandler(object):

    def __init__(self, conf, inputs, outputs):
        self.conf = conf
        self.inputs = inputs
        self.outputs = outputs

        # Create a config parser
        self.config = ConfigParser()
        self.config.read('ModisTimeSeries.ini')

    def extract_values(self, input_coords, epsg):

        # Reproject the input coordinates to the MODIS sinusoidal projection
        coords = self._reproject_coordinates(input_coords, epsg)

        # Get the path to the MODIS tile
        modis_file = self._get_tile(coords)

        # Log this request to the spatial file logger
        # Get the IP
        ip = cgi.escape(os.environ["REMOTE_ADDR"])
        # Log the cordinates
        spatiallog.info("%s,%s,\"%s\",%s" % (input_coords[0], input_coords[1], ip, modis_file != None))

        if modis_file is not None:

            #array_int_values = self._get_value_from_gdal(coords, modis_file)
            array_int_values = self._get_random_values()

            self.outputs['timeseries']['value'] = json.dumps({"success": True, "data": array_int_values})
            return SERVICE_SUCCEEDED

        else:

            self.outputs['timeseries']['value'] = json.dumps({"success": False, "data": None})
            return SERVICE_FAILED


    def get_time_series(self, input_coords, epsg, mimeType, width=512, height=512):

        # Reproject the input coordinates to the MODIS sinusoidal projection
        coords = self._reproject_coordinates(input_coords, epsg)

        # Get the path to the MODIS tile
        modis_file = self._get_tile(coords)
        
        # Log this request to the spatial file logger
        # Get the IP
        ip = cgi.escape(os.environ["REMOTE_ADDR"])
        # Log the cordinates
        spatiallog.info("%s,%s,\"%s\",%s" % (input_coords[0], input_coords[1], ip, modis_file != None))

        if modis_file is not None:

            #array_int_values = self._get_value_from_gdal(coords, modis_file)
            array_int_values = self._get_random_values()

            self.outputs['timeseries']['value'] = json.dumps(self._create_bfast_plot(array_int_values, width, height))
            return SERVICE_SUCCEEDED

        else:

            self.outputs['timeseries']['value'] = json.dumps(self._create_empty_image(width, height))
            return SERVICE_SUCCEEDED

    def _get_tile(self, coords):
        """
        Get the directory path to the requested MODIS subtile using a spatially
        enabled database.
        """

        # Get the SQLAlchemy URL from the configuration
        sqlalchemy_url = self.config.get('main', 'sqlalchemy.url')
        # the MODIS data directory
        modis_datadir = self.config.get('main', 'modis.datadir')
        # and the custom CRS for the MODIS sinusoidal projection.
        custom_crs = self.config.getint('main', 'custom.crs')

        # Engine, which the Session will use for connection resources
        engine = create_engine(sqlalchemy_url, module=spatialite)
        # Create a configured "Session" class
        Session = sessionmaker(bind=engine)
        # Create a Session
        session = Session()

        # Create a point from the requested coordinates
        p = WKTSpatialElement('POINT(%s %s)' % coords, custom_crs)

        countryConditions = []
        for country in session.query(ModisAvailableCountry).filter(ModisAvailableCountry.available == True):
            countryConditions.append(spfunc.within(p, country.geometry))

        tile = session.query(ModisExtent.name)\
            .filter(ModisExtent.available == True)\
            .filter(spfunc.within(p, ModisExtent.geometry))\
            .filter(or_(*countryConditions))\
            .first()
        if tile is not None:
            modis_file = "/%s/%s/NDVI.tif" % (modis_datadir, tile.name)
            return modis_file

        else:
            # Return None if there is no MODIS tile available for the requested
            # coordinates.
            return None
	
    def _get_value_from_gdal(self, coords, datadir):
    
        # start timing
        startTime = time.time()
        # coordinates to get pixel values for
        x = coords[0]
        y = coords[1]
        # Register GeoTIFF driver
        driver = gdal.GetDriverByName('GTiff')
        driver.Register()

        result = []

        # open the image
        log.debug("Accessing file: %s" % datadir)
        ds = gdal.Open(str(datadir), GA_ReadOnly)
        if ds is None:
            log.warn('Could not open image: %s' % str(datadir))
            sys.exit(1)

        # get image size
        #rows = ds.RasterYSize
        #cols = ds.RasterXSize
        bands = ds.RasterCount
        # get georeference info
        transform = ds.GetGeoTransform()
        xOrigin = transform[0]
        yOrigin = transform[3]
        pixelWidth = transform[1]
        pixelHeight = transform[5]

        # compute pixel offset
        xOffset = int((x - xOrigin) / pixelWidth)
        yOffset = int((y - yOrigin) / pixelHeight)
        # loop through the bands
        for j in range(bands):
            band = ds.GetRasterBand(j + 1) # 1-based index

            # read data and add the value to the string
            data = band.ReadAsArray(xOffset, yOffset, 1, 1)
            value = data[0, 0]
            result.append(int(value))

        endTime = time.time()
        # figure out how long the script took to run
        log.debug('It took ' + str(endTime - startTime) + ' seconds to read the input raster file.')

        return result

    def _reproject_coordinates(self, coords, epsg_code):
        """
        Reproject the requested coordinates to MODIS sinusoidal projection. EPSG
        code of input CRS must be known to GDAL.
        """

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(epsg_code))

        sinusoidalSrs = osr.SpatialReference()
        # From spatialreference.org: http://spatialreference.org/ref/sr-org/6842/
        sinusoidalSrs.ImportFromProj4("+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +a=6371007.181 +b=6371007.181 +units=m +no_defs")

        ct = osr.CoordinateTransformation(srs, sinusoidalSrs)

        (x, y, z) = ct.TransformPoint(float(coords[0]), float(coords[1]))

        return (x, y)



    def _create_bfast_plot(self, data_array, width, height):
        """
        Create a plot with R
        """

        # Start R timing
        startTime = time.time()

        rinterface.initr()

        r = robjects.r
        grdevices = importr('grDevices')

        # Import the bfast package
        bfast = importr('bfast')

        b = robjects.FloatVector(data_array)

        # arry by b to time serie vector
        b_ts = r.ts(b, start=robjects.IntVector([2000, 4]), frequency=23)

        # calculate bfast
        h = 23.0 / float(len(b_ts))
        b_bfast = r.bfast(b_ts, h=h, season="harmonic", max_iter=2)

        # Get the index names of the ListVector b_bfast
        names = b_bfast.names
        log.debug(names)

        temp_datadir = self.config.get('main', 'temp.datadir')
        temp_url = self.config.get('main', 'temp.url')
        file = NamedTemporaryFile(suffix=".png", dir=temp_datadir, delete=False)

        log.debug(file.name)
        grdevices.png(file=file.name, width=width, height=height)
        # Plotting code here
        r.par(col="black")
        r.plot(b_bfast)
        # Close the device
        grdevices.dev_off()

        # End R timing and log it
        endTime = time.time()
        log.debug('It took ' + str(endTime - startTime) + ' seconds to initalize R and draw a plot.')

        file.close()

        result = {"file": "%s/%s" % (temp_url, file.name.split("/")[-1])}
        try:
            result['magnitude'] = str(tuple(b_bfast[names.index("Magnitude")])[0])
        except ValueError:
            pass
        try:
            result['time'] = str(tuple(b_bfast[names.index("Time")])[0])
        except ValueError:
            pass

        return result

    def _create_empty_image(self, image_width, image_height):

        # Check pycairo capabilities
        if not (cairo.HAS_IMAGE_SURFACE and cairo.HAS_PNG_FUNCTIONS):
            raise HTTPBadRequest('cairo was not compiled with ImageSurface and PNG support')

        # Create a new cairo surface
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(image_width), int(image_height))

        ctx = cairo.Context(surface)

        text = "No imagery available for requested coordinates."

        x_bearing, y_bearing, width, height, x_advance, y_advance = ctx.text_extents (text)

        ctx.move_to((image_width / 2) - (width / 2), (image_height / 2) + (height / 2))
        ctx.set_source_rgba(0, 0, 0, 0.85)
        ctx.show_text(text)

        temp_datadir = self.config.get('main', 'temp.datadir')
        temp_url = self.config.get('main', 'temp.url')
        file = NamedTemporaryFile(suffix=".png", dir=temp_datadir, delete=False)
        surface.write_to_png(file)
        file.close()

        return {"file": "%s/%s" % (temp_url, file.name.split("/")[-1])}

    def _get_random_values(self):
        """
        A method only used during the development to replace the method
        _get_value_from_gdal
        """

        return [random() for i in range(322)]

def PlotTimeSeries(conf, inputs, outputs):

    try:
        log.debug(inputs['timeseries']['value'])
        timeseries = json.loads(str(inputs['timeseries']['value']).strip())
    except ValueError: # as e:
        #log.debug(e)
        conf["lenv"]["message"] = "Parameter \"timeseries\" is not valid or not supported."
        return SERVICE_FAILED

    try:
        imageWidth = int(inputs['width']['value'])
    except ValueError:
        imageWidth = 1024
    try:
        imageHeight = int(inputs['height']['value'])
    except ValueError:
        imageHeight = 512

    handler = PlotTimeSeriesHandler(conf, inputs, outputs)
    result = handler.plot(timeseries["data"], imageWidth, imageHeight)

    return result

class PlotTimeSeriesHandler():

    def __init__(self, conf, inputs, outputs):
        self.conf = conf
        self.inputs = inputs
        self.outputs = outputs

        # Create a config parser
        self.config = ConfigParser()
        self.config.read('ModisTimeSeries.ini')

    def plot(self, data_array, width, height):
        """
        Create a plot with R
        """

        # Start R timing
        startTime = time.time()
        
        rinterface.initr()

        r = robjects.r
        grdevices = importr('grDevices')

        vector = robjects.FloatVector(data_array)
        
        temp_datadir = self.config.get('main', 'temp.datadir')
        temp_url = self.config.get('main', 'temp.url')
        file = NamedTemporaryFile(suffix=".png", dir=temp_datadir, delete=False)

        grdevices.png(file=file.name, width=width, height=height)
        # Plotting code here
        r.par(col="black")
        r.plot(vector, xlab="Image Nr", ylab="Values", main="", type="l")
        # Close the device
        grdevices.dev_off()

        file.close()

        # End R timing and log it
        endTime = time.time()
        log.debug('It took ' + str(endTime - startTime) + ' seconds to initalize R and draw a plot.')

        self.outputs['plot']['value'] = json.dumps({"file": "%s/%s" % (temp_url, file.name.split("/")[-1])})
        return SERVICE_SUCCEEDED