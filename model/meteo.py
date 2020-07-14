#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# PCR-GLOBWB (PCRaster Global Water Balance) Global Hydrological Model
#
# Copyright (C) 2016, Edwin H. Sutanudjaja, Rens van Beek, Niko Wanders, Yoshihide Wada, 
# Joyce H. C. Bosmans, Niels Drost, Ruud J. van der Ent, Inge E. M. de Graaf, Jannis M. Hoch, 
# Kor de Jong, Derek Karssenberg, Patricia López López, Stefanie Peßenteiner, Oliver Schmitz, 
# Menno W. Straatsma, Ekkamol Vannametee, Dominik Wisser, and Marc F. P. Bierkens
# Faculty of Geosciences, Utrecht University, Utrecht, The Netherlands
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import calendar
import math

from pcraster.framework import *
import pcraster as pcr

import logging
logger = logging.getLogger(__name__)

import virtualOS as vos
from ncConverter import *

import evaporation.hamonETPFunctions as hamon_et0
import evaporation.ref_pot_et_penman_monteith as penman_monteith
import evaporation.shortwave_radiation as sw_rad

class Meteo(object):

    def __init__(self,iniItems,landmask,spinUp):
        object.__init__(self)

        self.cloneMap = iniItems.cloneMap
        self.tmpDir = iniItems.tmpDir
        self.inputDir = iniItems.globalOptions['inputDir']
        
        # landmask/area of interest
        self.landmask = landmask
        if iniItems.globalOptions['landmask'] != "None":
           self.landmask = vos.readPCRmapClone(\
           iniItems.globalOptions['landmask'],
           self.cloneMap,self.tmpDir,self.inputDir) 

        # option to ignore snow (temperature will be set to 25 deg C if this option is activated)
        self.ignore_snow = False
        if 'ignoreSnow' in list(iniItems.meteoOptions.keys()) and iniItems.meteoOptions['ignoreSnow'] == "True":
            self.ignore_snow = True

        self.preFileNC = iniItems.meteoOptions['precipitationNC']        # starting from 19 Feb 2014, we only support netcdf input files
        self.tmpFileNC = iniItems.meteoOptions['temperatureNC']

        self.refETPotMethod = iniItems.meteoOptions['referenceETPotMethod']
        msg = "Method for the reference potential evaporation: " + str(self.refETPotMethod)
        logger.info(msg)
        
        # inititate Penman-Monteith class
        if self.refETPotMethod == 'Penman-Monteith': 
            self.penman_monteith = penman_monteith.penmanMonteithET(windHeight = 10.00)
            msg = 'The Penman Monteith is instantiated for wind input data at 10 m height.'
            logger.info(msg)
            # TODO: Make flexible windHeight
        
        if self.refETPotMethod == 'Input': self.etpFileNC = iniItems.meteoOptions['refETPotFileNC']              

        # list of extra meteo variable names, needed for the Peman-Monteith calculation
        self.extra_meteo_var_names = ['wind_speed_10m',\
                                      'wind_speed_10m_u_comp',\
                                      'wind_speed_10m_v_comp',\
                                      'atmospheric_pressure',\
                                      'extraterestrial_radiation',\
                                      'shortwave_radiation',\
                                      'surface_net_solar_radiation',\
                                      'albedo',\
                                      'air_temperature_max',\
                                      'air_temperature_min',\
                                      'dewpoint_temperature_avg']


        #-----------------------------------------------------------------------            
        # NOTE: RvB 13/07/2016 Added correction constant and factor and variable name
        # to allow for easier use of netCDF climate inpute files
        # EHS 20/08/2016 modified for more flexibilities.  
        # - meteo conversion factors
        self.preConst       = 0.0
        self.preFactor      = 1.0
        self.tmpConst       = 0.0
        self.tmpFactor      = 1.0
        self.refETPotConst  = 0.0
        self.refETPotFactor = 1.0
        self.read_meteo_conversion_factors(iniItems.meteoOptions)
        # - variable names      
        self.preVarName      = 'precipitation' 
        self.tmpVarName      = 'temperature'
        self.refETPotVarName = 'evapotranspiration'
        self.read_meteo_variable_names(iniItems.meteoOptions)

        # latitudes (required for the Hamon and Penman-Monteith method)
        self.latitudes = pcr.ycoordinate(self.cloneMap) # needed to calculate 'referenceETPot'
        self.latitudes_in_radian = vos.deg2rad(self.latitudes)    
        
        # initiate shortwave radiation class, required for the Bristow-Campbell method
        self.sw_rad_based_on_bristow_campbell = False
        if ('shortwave_radiation' in iniItems.meteoOptions) and (iniItems.meteoOptions['shortwave_radiation'] == "Bristow-Campbell"):
            
            self.sw_rad_based_on_bristow_campbell = True

            msg = "The shortwave (solar) radiation will be estimated based on actual shortwave radiation is estimated based on an adaptation of the Bristow-Campbell model by Winslow et al (2001)"
            logger,info(msg)
            
            # read dem (should be in the same resolution as other meteo inputs)
            elevation_meteo = pcr.ifthen(self.landmask, \
                                         pcr.cover(vos.readPCRmapClone(meteoOptions['dem_for_input_meteo'], self.cloneMap, self.tmpDir, self.inputDir), 0.0))

            # read long term annual temperature and diurnal difference
            self.delta_temp_mean = pcr.ifthen(self.landmask, \
                                   pcr.cover(vos.readPCRmapClone(meteoOptions['annualDiurnalDeltaTmpIni'], self.cloneMap, self.tmpDir, self.inputDir), 0.0))
            self.temp_annual     = pcr.ifthen(self.landmask, \
                                   pcr.cover(vos.readPCRmapClone(meteoOptions['annualMeanTemperatureIni'], self.cloneMap, self.tmpDir, self.inputDir), 0.0))
            #
            # - read and apply conversion factors
            for meteo_var_name in ['delta_temp_mean',\
                                   'temp_annual']:
                #
                # - read constant
                consta_var_name = 'consta_' + meteo_var_name
                vars(self)[consta_var_name]     = pcr.spatial(pcr.scalar(0.0))
                if consta_var_name in meteoOptions:
                    vars(self)[consta_var_name] = pcr.cover(vos.readPCRmapClone(meteoOptions[consta_var_name], self.cloneMap, self.tmpDir, self.inputDir), 0.0)
                #
                # - read factor
                factor_var_name = 'factor_' + meteo_var_name
                vars(self)[factor_var_name]     = pcr.spatial(pcr.scalar(1.0))
                if factor_var_name in meteoOptions:
                    vars(self)[factor_var_name] = pcr.cover(vos.readPCRmapClone(meteoOptions[factor_var_name], self.cloneMap, self.tmpDir, self.inputDir), 1.0)
                #
                # - apply conversion factors
                vars(self)[meteo_var_name] = vars(self)[consta_var_name] + vars(self)[factor_var_name] * pcr.ifthen(self.landmask, vars(self)[meteo_var_name])

            # TODO: The long term mean annual and diurnal difference temperature should be calculated online. 
            
            # initiate short wave radiation class with the the solar constant = 118.1 MJ/m2/day
            self.sw_rad_model = sw_rad.ShortwaveRadiation(latitude        = self.latitudes, \
                                                          elevation       = elevation_meteo, \
                                                          temp_annual     = self.temp_annual, \
                                                          delta_temp_mean = self.temp_annual, \
                                                          solar_constant  = 118.1)

            #~ # initiate short wave radiation class with the the solar constant = 1362 W.m-2
            #~ self.sw_rad_model = sw_rad.ShortwaveRadiation(latitude        = self.latitudes, \
                                                          #~ elevation       = elevation_meteo, \
                                                          #~ temp_annual     = self.temp_annual, \
                                                          #~ delta_temp_mean = self.temp_annual, \
                                                          #~ solar_constant  = 1362.0)

            # - TODO: set solar_constant in the configuration file                                              

        # daily time step
        self.usingDailyTimeStepForcingData = False
        if iniItems.timeStep == 1.0 and iniItems.timeStepUnit == "day":
            self.usingDailyTimeStepForcingData = True
        
        # forcing downscaling options:
        self.forcingDownscalingOptions(iniItems)

        # option to use netcdf files that are defined per year (one file for each year)
        self.precipitation_set_per_year  = iniItems.meteoOptions['precipitation_set_per_year'] == "True"
        self.temperature_set_per_year    = iniItems.meteoOptions['temperature_set_per_year'] == "True"
        self.refETPotFileNC_set_per_year = iniItems.meteoOptions['refETPotFileNC_set_per_year'] == "True" 
        
        # make the iniItems available for the other modules:
        self.iniItems = iniItems
        
        self.report = True
        try:
            self.outDailyTotNC = iniItems.meteoOptions['outDailyTotNC'].split(",")
            self.outMonthTotNC = iniItems.meteoOptions['outMonthTotNC'].split(",")
            self.outMonthAvgNC = iniItems.meteoOptions['outMonthAvgNC'].split(",")
            self.outMonthEndNC = iniItems.meteoOptions['outMonthEndNC'].split(",")
            self.outAnnuaTotNC = iniItems.meteoOptions['outAnnuaTotNC'].split(",")
            self.outAnnuaAvgNC = iniItems.meteoOptions['outAnnuaAvgNC'].split(",")
            self.outAnnuaEndNC = iniItems.meteoOptions['outAnnuaEndNC'].split(",")
        except:
            self.report = False
        if self.report == True:
            # daily output in netCDF files:
            self.outNCDir  = iniItems.outNCDir
            self.netcdfObj = PCR2netCDF(iniItems)
            #
            if self.outDailyTotNC[0] != "None":
                for var in self.outDailyTotNC:
                    # creating the netCDF files:
                    self.netcdfObj.createNetCDF(str(self.outNCDir)+"/"+ \
                                                str(var)+"_dailyTot.nc",\
                                                    var,"undefined")
            # MONTHly output in netCDF files:
            # - cummulative
            if self.outMonthTotNC[0] != "None":
                for var in self.outMonthTotNC:
                    # initiating monthlyVarTot (accumulator variable):
                    vars(self)[var+'MonthTot'] = None
                    # creating the netCDF files:
                    self.netcdfObj.createNetCDF(str(self.outNCDir)+"/"+ \
                                                str(var)+"_monthTot.nc",\
                                                    var,"undefined")
            # - average
            if self.outMonthAvgNC[0] != "None":
                for var in self.outMonthAvgNC:
                    # initiating monthlyTotAvg (accumulator variable)
                    vars(self)[var+'MonthTot'] = None
                    # initiating monthlyVarAvg:
                    vars(self)[var+'MonthAvg'] = None
                     # creating the netCDF files:
                    self.netcdfObj.createNetCDF(str(self.outNCDir)+"/"+ \
                                                str(var)+"_monthAvg.nc",\
                                                    var,"undefined")
            # - last day of the month
            if self.outMonthEndNC[0] != "None":
                for var in self.outMonthEndNC:
                     # creating the netCDF files:
                    self.netcdfObj.createNetCDF(str(self.outNCDir)+"/"+ \
                                                str(var)+"_monthEnd.nc",\
                                                    var,"undefined")
            # YEARly output in netCDF files:
            # - cummulative
            if self.outAnnuaTotNC[0] != "None":
                for var in self.outAnnuaTotNC:
                    # initiating yearly accumulator variable:
                    vars(self)[var+'AnnuaTot'] = None
                    # creating the netCDF files:
                    self.netcdfObj.createNetCDF(str(self.outNCDir)+"/"+ \
                                                str(var)+"_annuaTot.nc",\
                                                    var,"undefined")
            # - average
            if self.outAnnuaAvgNC[0] != "None":
                for var in self.outAnnuaAvgNC:
                    # initiating annualyVarAvg:
                    vars(self)[var+'AnnuaAvg'] = None
                    # initiating annualyTotAvg (accumulator variable)
                    vars(self)[var+'AnnuaTot'] = None
                     # creating the netCDF files:
                    self.netcdfObj.createNetCDF(str(self.outNCDir)+"/"+ \
                                                str(var)+"_annuaAvg.nc",\
                                                    var,"undefined")
            # - last day of the year
            if self.outAnnuaEndNC[0] != "None":
                for var in self.outAnnuaEndNC:
                     # creating the netCDF files:
                    self.netcdfObj.createNetCDF(str(self.outNCDir)+"/"+ \
                                                str(var)+"_annuaEnd.nc",\
                                                    var,"undefined")


    def read_meteo_conversion_factors(self, meteoOptions):

        # conversion constants and factors for default meteo variables: precipitation, temperature and reference potential evaporation 
        if 'precipitationConstant' in meteoOptions: self.preConst       = pcr.cover(vos.readPCRmapClone(meteoOptions['precipitationConstant'], self.cloneMap, self.tmpDir, self.inputDir), 0.0)
        if 'precipitationFactor'   in meteoOptions: self.preFactor      = pcr.cover(vos.readPCRmapClone(meteoOptions['precipitationFactor'  ], self.cloneMap, self.tmpDir, self.inputDir), 1.0)
        if 'temperatureConstant'   in meteoOptions: self.tmpConst       = pcr.cover(vos.readPCRmapClone(meteoOptions['temperatureConstant'  ], self.cloneMap, self.tmpDir, self.inputDir), 0.0)
        if 'temperatureFactor'     in meteoOptions: self.tmpFactor      = pcr.cover(vos.readPCRmapClone(meteoOptions['temperatureFactor'    ], self.cloneMap, self.tmpDir, self.inputDir), 1.0)
        if 'referenceEPotConstant' in meteoOptions: self.refETPotConst  = pcr.cover(vos.readPCRmapClone(meteoOptions['referenceEPotConstant'], self.cloneMap, self.tmpDir, self.inputDir), 0.0)
        if 'referenceEPotFactor'   in meteoOptions: self.refETPotFactor = pcr.cover(vos.readPCRmapClone(meteoOptions['referenceEPotFactor'  ], self.cloneMap, self.tmpDir, self.inputDir), 1.0)
        
        # conversion constants and factors for extra meteo variables 
        for meteo_var_name in self.extra_meteo_var_names:
            # constant
            consta_var_name = 'consta_for_' + meteo_var_name
            vars(self)[consta_var_name]     = pcr.spatial(pcr.scalar(0.0))
            if consta_var_name in meteoOptions:
                vars(self)[consta_var_name] = pcr.cover(vos.readPCRmapClone(meteoOptions[consta_var_name], self.cloneMap, self.tmpDir, self.inputDir), 0.0)
            # factor
            factor_var_name = 'factor_for_' + meteo_var_name
            vars(self)[factor_var_name]     = pcr.spatial(pcr.scalar(1.0))
            if factor_var_name in meteoOptions:
                vars(self)[factor_var_name] = pcr.cover(vos.readPCRmapClone(meteoOptions[factor_var_name], self.cloneMap, self.tmpDir, self.inputDir), 1.0)
        


    def read_meteo_variable_names(self, meteoOptions):

        if 'precipitationVariableName' in meteoOptions: self.preVarName      = meteoOptions['precipitationVariableName']
        if 'temperatureVariableName'   in meteoOptions: self.tmpVarName      = meteoOptions['temperatureVariableName'  ]
        if 'referenceEPotVariableName' in meteoOptions: self.refETPotVarName = meteoOptions['referenceEPotVariableName']

    def forcingDownscalingOptions(self, iniItems):

        self.downscalePrecipitationOption  = False
        self.downscaleTemperatureOption    = False
        self.downscaleReferenceETPotOption = False

        if 'meteoDownscalingOptions' in iniItems.allSections:

            # downscaling options
            if iniItems.meteoDownscalingOptions['downscalePrecipitation']  == "True":
                self.downscalePrecipitationOption  = True  
                logger.info("Precipitation forcing will be downscaled to the cloneMap resolution.")

            if iniItems.meteoDownscalingOptions['downscaleTemperature']    == "True":
                self.downscaleTemperatureOption    = True  
                logger.info("Temperature forcing will be downscaled to the cloneMap resolution.")

            #~ if iniItems.meteoDownscalingOptions['downscaleReferenceETPot'] == "True" and self.refETPotMethod != 'Hamon':
            if iniItems.meteoDownscalingOptions['downscaleReferenceETPot'] == "True":
                self.downscaleReferenceETPotOption = True 
                logger.info("Reference potential evaporation will be downscaled to the cloneMap resolution.")

                # Note that for the Hamon method: referencePotET will be calculated based on temperature,  
                # therefore, we may not have to downscale it (particularly if temperature is already provided at high resolution). 

        if self.downscalePrecipitationOption or\
           self.downscaleTemperatureOption   or\
           self.downscaleReferenceETPotOption:

            # cellArea (m2), needed for downscaling P and ET0
            if 'cellAreaMap' not in list(iniItems.meteoOptions.keys()):
                iniItems.meteoOptions['cellAreaMap'] = iniItems.routingOptions['cellAreaMap']
            cellArea = vos.readPCRmapClone(\
                iniItems.meteoOptions['cellAreaMap'],
                self.cloneMap,self.tmpDir,self.inputDir)
            self.cellArea = pcr.ifthen(self.landmask, cellArea)

            # creating anomaly DEM
            highResolutionDEM = vos.readPCRmapClone(\
               iniItems.meteoDownscalingOptions['highResolutionDEM'],
               self.cloneMap,self.tmpDir,self.inputDir)
            highResolutionDEM = pcr.cover(highResolutionDEM, 0.0)
            highResolutionDEM = pcr.max(highResolutionDEM, 0.0)
            self.meteoDownscaleIds = vos.readPCRmapClone(\
               iniItems.meteoDownscalingOptions['meteoDownscaleIds'],
               self.cloneMap,self.tmpDir,self.inputDir,isLddMap=False,cover=None,isNomMap=True)
            self.cellArea = vos.readPCRmapClone(\
               iniItems.routingOptions['cellAreaMap'],
               self.cloneMap,self.tmpDir,self.inputDir)
            loweResolutionDEM = pcr.areatotal(pcr.cover(highResolutionDEM*self.cellArea, 0.0),\
                                              self.meteoDownscaleIds)/\
                                pcr.areatotal(pcr.cover(self.cellArea, 0.0),\
                                              self.meteoDownscaleIds)                  
            self.anomalyDEM = highResolutionDEM - loweResolutionDEM    # unit: meter  

            # temperature lapse rate (netCDF) file 
            self.temperLapseRateNC = vos.getFullPath(iniItems.meteoDownscalingOptions[\
                                        'temperLapseRateNC'],self.inputDir)                         
            self.temperatCorrelNC  = vos.getFullPath(iniItems.meteoDownscalingOptions[\
                                        'temperatCorrelNC'],self.inputDir)                    # TODO: Remove this criteria.                         

            # precipitation lapse rate (netCDF) file 
            self.precipLapseRateNC = vos.getFullPath(iniItems.meteoDownscalingOptions[\
                                        'precipLapseRateNC'],self.inputDir)
            self.precipitCorrelNC  = vos.getFullPath(iniItems.meteoDownscalingOptions[\
                                        'precipitCorrelNC'],self.inputDir)                    # TODO: Remove this criteria.                           

        else:
            logger.info("No forcing downscaling is implemented.")

        # forcing smoothing options: - THIS is still experimental. PS: MUST BE TESTED.
        self.forcingSmoothing = False
        if 'meteoDownscalingOptions' in iniItems.allSections and \
           'smoothingWindowsLength' in list(iniItems.meteoDownscalingOptions.keys()):

            if float(iniItems.meteoDownscalingOptions['smoothingWindowsLength']) > 0.0:
                self.forcingSmoothing = True
                self.smoothingWindowsLength = vos.readPCRmapClone(\
                   iniItems.meteoDownscalingOptions['smoothingWindowsLength'],
                   self.cloneMap,self.tmpDir,self.inputDir)
                msg = "Forcing data will be smoothed with 'windowaverage' using the window length:"+str(iniItems.meteoDownscalingOptions['smoothingWindowsLength'])
                logger.info(msg)   
 
    def perturb(self, name, **parameters):

        if name == "precipitation":

            # perturb the precipitation
            self.precipitation = self.precipitation * \
            pcr.min(pcr.max((1 + mapnormal() * parameters['standard_deviation']),0.01),2.0)
            #TODO: Please also make sure that precipitation >= 0
            #TODO: Add minimum and maximum 

        else:
            print("Error: only precipitation may be updated at this time")
            return -1


    def update(self, currTimeStep):

        # Downscaling precipitation
        self.precipitation_before_downscaling = pcr.ifthen(self.landmask, self.precipitation)
        if self.downscalePrecipitationOption: self.downscalePrecipitation(currTimeStep)

        # dowsncaling temperature        
        self.temperature_before_downscaling = pcr.ifthen(self.landmask, self.temperature)
        if self.downscaleTemperatureOption: self.downscaleTemperature(currTimeStep)

        # calculate or obtain referencePotET
        if self.refETPotMethod == 'Hamon':
            
            msg = "Calculating reference potential evaporation based on the Hamon method"
            logger.info(msg)

            self.referencePotET = hamon_et0.HamonPotET(self.temperature,\
                                                       currTimeStep.doy,\
                                                       self.latitudes)

        if self.refETPotMethod == 'Penman-Monteith':
            
            msg = "Calculating reference potential evaporation based on the Penman-Monteith"
            logger.info(msg)
            
            # extraterestrial radiation
            
            if ('extraterestrial_radiation' not in list(self.iniItems.meteoOptions.keys())) or \
                                                       (self.iniItems.meteoOptions['extraterestrial_radiation'] == "None"):
                
                msg = "Estimating extraterestrial radiation based on Dingman's Physical Geography (2015)"
                logger.info(msg)
                
                # get the day angle (rad)
                # - julian day
                julian_day    = currTimeStep.doy
                #~ julian_day = penman_monteith.shortwave_radiation.get_julian_day_number(currTimeStep._currTimeFull)
                # - number of days in a year
                number_days = 365
                if calendar.isleap(currTimeStep.year): number_days = 366
                # - day angle (rad)
                day_angle = float(julian_day - 1) / number_days * 2 * math.pi

                # solar declination
                solar_declination = sw_rad.compute_solar_declination(day_angle)
                
                # eccentricity 
                eccentricity = sw_rad.compute_eccentricity(day_angle)
                
                # day length (hours)
                day_length = sw_rad.compute_day_length(latitude = self.latitudes_in_radian,\
                                                       solar_declination = solar_declination)
                
                # extraterestrial_radiation
                extraterestrial_radiation = sw_rad.compute_radsw_ext(latitude = self.latitudes_in_radian, \
                                                                                solar_declination = solar_declination, \
                                                                                eccentricity = eccentricity, \
                                                                                day_length = day_length, \
                                                                                solar_constant = 118.1)
                # TODO: UNTIL-THIS-PART check deg and rad values
                
                # TODO: set solar_constant in the configuration file                                              

                # extraterestrial_radiation (unit: J.m-2.day-1)
                self.extraterestrial_radiation = extraterestrial_radiation * 1e6
                
            else:

                msg = "Extraterestrial shortwave (solar) radiation is obtained from the input file."
                logger.info(msg)

            #~ # debug
            #~ pcr.aguila(self.extraterestrial_radiation)
            #~ input("Press Enter to continue...")
            #~ os.system("killall aguila")

            # shortwave radiation
            
            if self.iniItems.meteoOptions['shortwave_radiation'].endswith(('.nc', '.nc4', '.nc3')):

                msg = "Shortwave (solar) radiation is obtained from the input file."
                logger.info(msg)
                

            if self.iniItems.meteoOptions['shortwave_radiation'] == "None":
        
                msg = "Estimating shortwave (solar) radiation based on the input of net radiation and albedo."
                logger.info(msg)
                
                self.shortwave_radiation = self.surface_net_solar_radiation / (pcr.spatial(pcr.scalar(1.0)) - self.albedo)
                
            #~ # debug
            #~ pcr.aguila(self.shortwave_radiation)
            #~ input("Press Enter to continue...")
            #~ os.system("killall aguila")

            if self.iniItems.meteoOptions['shortwave_radiation'] == "Bristow-Campbell":

                msg = "Estimating shortwave (solar) radiation based on an adaptation of the Bristow-Campbell model by Winslow et al (2001)."
                logger.info(msg)
                
                self.sw_rad_model.update(date = currTimeStep._currTimeFull, \
                                         prec_daily = self.precipitation, \
                                         temp_min_daily  = self.air_temperature_min, \
                                         temp_max_daily  = self.air_temperature_max, \
                                         temp_avg_daily  = self.temperature, \
                                         dew_temperature = self.dewpoint_temperature_avg, \
                                         extraterrestrial_rad = self.extraterestrial_radiation / 1000000.)
                
                # using the values from the shortwave radiation model (unit: J.m-2.day-1)
                self.shortwave_radiation       = self.sw_rad_model.radsw_act * 1e6
                self.extraterestrial_radiation = self.sw_rad_model.radsw_ext * 1e6
            
            # wind speed (m.s-1)
            if ('wind_speed_10m' not in list(self.iniItems.meteoOptions.keys())) or \
                                            (self.iniItems.meteoOptions['wind_speed_10m'] == "None"): 
                msg = "Calculating wind speed based on their u and v components"
                logger.info(msg)
                self.wind_speed_10m = (self.wind_speed_10m_u_comp**2. + self.wind_speed_10m_v_comp**2.)**(0.5)
            
            #~ # debug
            #~ pcr.aguila(self.shortwave_radiation)
            #~ pcr.aguila(self.extraterestrial_radiation)
            #~ pcr.aguila(self.wind_speed_10m)
            #~ input("Press Enter to continue...")
            #~ os.system("killall aguila")

            # update PM method
            
            msg = "Calculating reference potential evaporation based on Penman-Monteith."
            logger.info(msg)
            
            # calculate netRadiation (unit: W.m**-2)
            
            # - fraction of shortWaveRadiation (dimensionless)
            fractionShortWaveRadiation = pcr.cover(pcr.min(1.0, \
                                                  self.shortwave_radiation / self.extraterestrial_radiation), \
                                                  0.0)
            
            # - compute vapour pressure (Pa)
            vapourPressure = penman_monteith.getSaturatedVapourPressure(\
                                                                        self.dewpoint_temperature_avg)
            # - longwave radiation in W.m**-2
            longWaveRadiation = penman_monteith.getLongWaveRadiation(self.temperature, \
                                                                     vapourPressure, \
                                                                     fractionShortWaveRadiation)
           
            # - shortwave radiation in W.m**-2
            shortWaveRadiation = (self.shortwave_radiation / 1e6) / 0.0864
            
            # - netRadiation in W.m**-2)
            netRadiation = pcr.max(0.0, shortWaveRadiation- longWaveRadiation)
            
            # - referencePotET in m.day-1
            self.referencePotET = self.penman_monteith.updatePotentialEvaporation(netRadiation        = netRadiation, 
                                                                                  airTemperature      = self.temperature, 
                                                                                  windSpeed           = self.wind_speed_10m, 
                                                                                  atmosphericPressure = self.atmospheric_pressure,
                                                                                  unsatVapPressure    = vapourPressure, 
                                                                                  relativeHumidity    = None,\
                                                                                  timeStepLength      = 86400)

            # all radiation terms in W.m**-2
            self.extraterrestrialRadiation = (self.extraterestrial_radiation / 1e6) / 0.0864
            self.shorWaveRadiation         = shortWaveRadiation
            self.longWaveRadiation         = longWaveRadiation
            self.netRadiation              = netRadiation

        # Downscaling referenceETPot (based on temperature)
        self.referencePotET_before_downscaling = pcr.ifthen(self.landmask, self.referencePotET)
        if self.downscaleReferenceETPotOption: self.downscaleReferenceETPot()
 
        # smoothing:
        if self.forcingSmoothing == True:
            logger.debug("Forcing data are smoothed.")   
            self.precipitation  = pcr.windowaverage(self.precipitation , self.smoothingWindowsLength)
            self.temperature    = pcr.windowaverage(self.temperature   , self.smoothingWindowsLength)
            self.referencePotET = pcr.windowaverage(self.referencePotET, self.smoothingWindowsLength)
        
        # rounding temperature values to minimize numerical errors (note only to minimize, not remove)
        self.temperature   = pcr.roundoff(self.temperature*1000.)/1000. 
        
        # ignore snow by setting temperature to 25 deg C
        if self.ignore_snow: self.temperature = pcr.spatial(pcr.scalar(25.))
        
        # define precipitation, temperature and referencePotET ONLY at landmask area (for reporting):
        self.precipitation  = pcr.ifthen(self.landmask, self.precipitation)
        self.temperature    = pcr.ifthen(self.landmask, self.temperature)
        self.referencePotET = pcr.ifthen(self.landmask, self.referencePotET)

        # make sure precipitation and referencePotET are always positive:
        self.precipitation  = pcr.max(0.0, self.precipitation)
        self.referencePotET = pcr.max(0.0, self.referencePotET)

        if self.report == True:
            timeStamp = datetime.datetime(currTimeStep.year,\
                                          currTimeStep.month,\
                                          currTimeStep.day,\
                                          0)
            # writing daily output to netcdf files
            timestepPCR = currTimeStep.timeStepPCR
            if self.outDailyTotNC[0] != "None":
                for var in self.outDailyTotNC:
                    self.netcdfObj.data2NetCDF(str(self.outNCDir)+"/"+ \
                                         str(var)+"_dailyTot.nc",\
                                         var,\
                          pcr2numpy(self.__getattribute__(var),vos.MV),\
                                         timeStamp,timestepPCR-1)

            # writing monthly output to netcdf files
            # -cummulative
            if self.outMonthTotNC[0] != "None":
                for var in self.outMonthTotNC:

                    # introduce variables at the beginning of simulation or
                    #     reset variables at the beginning of the month
                    if currTimeStep.timeStepPCR == 1 or \
                       currTimeStep.day == 1:\
                       vars(self)[var+'MonthTot'] = pcr.scalar(0.0)

                    # accumulating
                    vars(self)[var+'MonthTot'] += vars(self)[var]

                    # reporting at the end of the month:
                    if currTimeStep.endMonth == True: 
                        self.netcdfObj.data2NetCDF(str(self.outNCDir)+"/"+ \
                                         str(var)+"_monthTot.nc",\
                                         var,\
                          pcr2numpy(self.__getattribute__(var+'MonthTot'),\
                           vos.MV),timeStamp,currTimeStep.monthIdx-1)
            # -average
            if self.outMonthAvgNC[0] != "None":
                for var in self.outMonthAvgNC:
                    # only if a accumulator variable has not been defined: 
                    if var not in self.outMonthTotNC: 

                        # introduce accumulator at the beginning of simulation or
                        #     reset accumulator at the beginning of the month
                        if currTimeStep.timeStepPCR == 1 or \
                           currTimeStep.day == 1:\
                           vars(self)[var+'MonthTot'] = pcr.scalar(0.0)
                        # accumulating
                        vars(self)[var+'MonthTot'] += vars(self)[var]

                    # calculating average & reporting at the end of the month:
                    if currTimeStep.endMonth == True:
                        vars(self)[var+'MonthAvg'] = vars(self)[var+'MonthTot']/\
                                                     currTimeStep.day  
                        self.netcdfObj.data2NetCDF(str(self.outNCDir)+"/"+ \
                                         str(var)+"_monthAvg.nc",\
                                         var,\
                          pcr2numpy(self.__getattribute__(var+'MonthAvg'),\
                           vos.MV),timeStamp,currTimeStep.monthIdx-1)
            #
            # -last day of the month
            if self.outMonthEndNC[0] != "None":
                for var in self.outMonthEndNC:
                    # reporting at the end of the month:
                    if currTimeStep.endMonth == True: 
                        self.netcdfObj.data2NetCDF(str(self.outNCDir)+"/"+ \
                                         str(var)+"_monthEnd.nc",\
                                         var,\
                          pcr2numpy(self.__getattribute__(var),vos.MV),\
                                         timeStamp,currTimeStep.monthIdx-1)

            # writing yearly output to netcdf files
            # -cummulative
            if self.outAnnuaTotNC[0] != "None":
                for var in self.outAnnuaTotNC:

                    # introduce variables at the beginning of simulation or
                    #     reset variables at the beginning of the month
                    if currTimeStep.timeStepPCR == 1 or \
                       currTimeStep.doy == 1:\
                       vars(self)[var+'AnnuaTot'] = pcr.scalar(0.0)

                    # accumulating
                    vars(self)[var+'AnnuaTot'] += vars(self)[var]

                    # reporting at the end of the year:
                    if currTimeStep.endYear == True: 
                        self.netcdfObj.data2NetCDF(str(self.outNCDir)+"/"+ \
                                         str(var)+"_annuaTot.nc",\
                                         var,\
                          pcr2numpy(self.__getattribute__(var+'AnnuaTot'),\
                           vos.MV),timeStamp,currTimeStep.annuaIdx-1)
            # -average
            if self.outAnnuaAvgNC[0] != "None":
                for var in self.outAnnuaAvgNC:
                    # only if a accumulator variable has not been defined: 
                    if var not in self.outAnnuaTotNC: 
                        # introduce accumulator at the beginning of simulation or
                        #     reset accumulator at the beginning of the year
                        if currTimeStep.timeStepPCR == 1 or \
                           currTimeStep.doy == 1:\
                           vars(self)[var+'AnnuaTot'] = pcr.scalar(0.0)
                        # accumulating
                        vars(self)[var+'AnnuaTot'] += vars(self)[var]
                    #
                    # calculating average & reporting at the end of the year:
                    if currTimeStep.endYear == True:
                        vars(self)[var+'AnnuaAvg'] = vars(self)[var+'AnnuaTot']/\
                                                     currTimeStep.doy  
                        self.netcdfObj.data2NetCDF(str(self.outNCDir)+"/"+ \
                                         str(var)+"_annuaAvg.nc",\
                                         var,\
                          pcr2numpy(self.__getattribute__(var+'AnnuaAvg'),\
                           vos.MV),timeStamp,currTimeStep.annuaIdx-1)
            #
            # -last day of the year
            if self.outAnnuaEndNC[0] != "None":
                for var in self.outAnnuaEndNC:
                    # reporting at the end of the year:
                    if currTimeStep.endYear == True: 
                        self.netcdfObj.data2NetCDF(str(self.outNCDir)+"/"+ \
                                         str(var)+"_annuaEnd.nc",\
                                         var,\
                          pcr2numpy(self.__getattribute__(var),vos.MV),\
                                         timeStamp,currTimeStep.annuaIdx-1)


    def downscalePrecipitation(self, currTimeStep, useFactor = True, minCorrelationCriteria = 0.85, considerCellArea = True, drizzle_limit = 0.001):
        
        # TODO: add CorrelationCriteria in the config file
        
        preSlope = 0.001 * vos.netcdf2PCRobjClone(\
                           self.precipLapseRateNC, 'precipitation',\
                           currTimeStep.month, useDoy = "Yes",\
                           cloneMapFileName=self.cloneMap,\
                           LatitudeLongitude = True)
        preSlope = pcr.cover(preSlope, 0.0)
        preSlope = pcr.max(0.,preSlope)
        
        preCriteria = vos.netcdf2PCRobjClone(\
                     self.precipitCorrelNC, 'precipitation',\
                     currTimeStep.month, useDoy = "Yes",\
                     cloneMapFileName=self.cloneMap,\
                     LatitudeLongitude = True)
        preSlope = pcr.ifthenelse(preCriteria > minCorrelationCriteria,\
                   preSlope, 0.0)             
        preSlope = pcr.cover(preSlope, 0.0)
    
        if useFactor == True:
            factor = pcr.max(0.,self.precipitation + preSlope * self.anomalyDEM)
            if considerCellArea: factor = factor * self.cellArea
            factor = factor / pcr.areaaverage(factor, self.meteoDownscaleIds)
            # - do not downscale drizzle
            #~ factor = pcr.ifthenelse(pcr.areaaverage(self.precipitation, self.meteoDownscaleIds) > drizzle_limit, factor, 1.00) 
            factor = pcr.ifthenelse(self.precipitation > drizzle_limit, factor, 1.00) 
            factor = pcr.cover(factor, 1.0)
            self.precipitation = factor * self.precipitation
        else:
            self.precipitation = self.precipitation + preSlope*self.anomalyDEM

        self.precipitation = pcr.max(0.0, self.precipitation)

    def downscaleTemperature(self, currTimeStep, useFactor = False, maxCorrelationCriteria = -0.75, zeroCelciusInKelvin = 273.15, considerCellArea = True):
        
        # TODO: add CorrelationCriteria in the config file

        tmpSlope = 1.000 * vos.netcdf2PCRobjClone(\
                           self.temperLapseRateNC, 'temperature',\
                           currTimeStep.month, useDoy = "Yes",\
                           cloneMapFileName=self.cloneMap,\
                           LatitudeLongitude = True)
        tmpSlope = pcr.min(0.,tmpSlope)  # must be negative
        tmpCriteria = vos.netcdf2PCRobjClone(\
                      self.temperatCorrelNC, 'temperature',\
                      currTimeStep.month, useDoy = "Yes",\
                      cloneMapFileName=self.cloneMap,\
                      LatitudeLongitude = True)
        tmpSlope = pcr.ifthenelse(tmpCriteria < maxCorrelationCriteria,\
                   tmpSlope, 0.0)             
        tmpSlope = pcr.cover(tmpSlope, 0.0)
    
        if useFactor == True:
            temperatureInKelvin = self.temperature + zeroCelciusInKelvin
            factor = pcr.max(0.0, temperatureInKelvin + tmpSlope * self.anomalyDEM)
            if considerCellArea: factor = factor * self.cellArea
            factor = factor / \
                     pcr.areaaverage(factor, self.meteoDownscaleIds)
            factor = pcr.cover(factor, 1.0)
            self.temperature = factor * temperatureInKelvin - zeroCelciusInKelvin
        else:
            self.temperature = self.temperature + tmpSlope * self.anomalyDEM

    def downscaleReferenceETPot(self, zeroCelciusInKelvin = 273.15, usingHamon = False, considerCellArea = True, julian_day = None, min_limit = 0.001):
        
        if usingHamon:
            # factor is based on hamon reference potential evaporation using high resolution temperature
            factor = hamon_et0.HamonPotET(self.temperature,\
                                          julian_day,\
                                          self.latitudes)
        else:
            # factor is based on high resolution temperature in Kelvin unit
            factor = self.temperature + zeroCelciusInKelvin
        
        factor = pcr.max(0.0, factor)
        if considerCellArea: factor = factor * self.cellArea
        
        factor = factor / \
                 pcr.areaaverage(factor, self.meteoDownscaleIds)

        # - do not downscale small values
        #~ factor = pcr.ifthenelse(pcr.areaaverage(self.referencePotET, self.meteoDownscaleIds) > min_limit, factor, 1.00) 
        factor = pcr.ifthenelse(self.referencePotET > min_limit, factor, 1.00) 
        factor = pcr.cover(factor, 1.0)

        factor = pcr.cover(factor, 1.0)
        
        self.referencePotET = pcr.max(0.0, factor * self.referencePotET)
        
        # UNTIL THIS PART

    def read_forcings(self,currTimeStep):

        #-----------------------------------------------------------------------
        # NOTE: RvB 13/07/2016 hard-coded reference to the variable names
        # preciptiation, temperature and evapotranspiration have been replaced
        # by the variable names used in the netCDF and passed from the ini file
        #-----------------------------------------------------------------------

        
        # method for finding time indexes in the precipitation netdf file:
        # - the default one
        method_for_time_index = None
        method_for_time_index = "daily"
        # - based on the ini/configuration file (if given)
        if 'time_index_method_for_precipitation_netcdf' in list(self.iniItems.meteoOptions.keys()) and\
                                                           self.iniItems.meteoOptions['time_index_method_for_precipitation_netcdf'] != "None":
            method_for_time_index = self.iniItems.meteoOptions['time_index_method_for_precipitation_netcdf']
        
        # reading precipitation:
        if self.precipitation_set_per_year:
            #~ print currTimeStep.year
            nc_file_per_year = self.preFileNC %(float(currTimeStep.year), float(currTimeStep.year))
            self.precipitation = vos.netcdf2PCRobjClone(\
                                      nc_file_per_year, self.preVarName,\
                                      str(currTimeStep.fulldate), 
                                      useDoy = method_for_time_index,
                                      cloneMapFileName = self.cloneMap,\
                                      LatitudeLongitude = True)
        else:
            self.precipitation = vos.netcdf2PCRobjClone(\
                                      self.preFileNC, self.preVarName,\
                                      str(currTimeStep.fulldate), 
                                      useDoy = method_for_time_index,
                                      cloneMapFileName = self.cloneMap,\
                                      LatitudeLongitude = True)

        #-----------------------------------------------------------------------
        # NOTE: RvB 13/07/2016 added to automatically update precipitation              
        self.precipitation  = self.preConst + self.preFactor * pcr.ifthen(self.landmask, self.precipitation)
        #-----------------------------------------------------------------------

        # make sure that precipitation is always positive
        self.precipitation = pcr.max(0., self.precipitation)
        self.precipitation = pcr.cover(  self.precipitation, 0.0)
        
        # ignore very small values of precipitation (less than 0.00001 m/day or less than 0.01 kg.m-2.day-1 )
        if self.usingDailyTimeStepForcingData:
            self.precipitation = pcr.rounddown(self.precipitation*100000.)/100000.

        
        # method for finding time index in the temperature netdf file:
        # - the default one
        method_for_time_index = None
        # - based on the ini/configuration file (if given)
        if 'time_index_method_for_temperature_netcdf' in list(self.iniItems.meteoOptions.keys()) and\
                                                         self.iniItems.meteoOptions['time_index_method_for_temperature_netcdf'] != "None":
            method_for_time_index = self.iniItems.meteoOptions['time_index_method_for_temperature_netcdf']

        # reading temperature
        if self.temperature_set_per_year:
            nc_file_per_year = self.tmpFileNC %(int(currTimeStep.year), int(currTimeStep.year))
            self.temperature = vos.netcdf2PCRobjClone(\
                                      nc_file_per_year, self.tmpVarName,\
                                      str(currTimeStep.fulldate), 
                                      useDoy = method_for_time_index,
                                      cloneMapFileName = self.cloneMap,\
                                      LatitudeLongitude = True)
        else:
            self.temperature = vos.netcdf2PCRobjClone(\
                                 self.tmpFileNC,self.tmpVarName,\
                                 str(currTimeStep.fulldate), 
                                 useDoy = method_for_time_index,
                                 cloneMapFileName=self.cloneMap,\
                                 LatitudeLongitude = True)

        #-----------------------------------------------------------------------
        # NOTE: RvB 13/07/2016 added to automatically update temperature
        self.temperature    = self.tmpConst + self.tmpFactor * pcr.ifthen(self.landmask, self.temperature)
        #-----------------------------------------------------------------------

        if self.refETPotMethod == 'Input': 

            # method for finding time indexes in the precipitation netdf file:
            # - the default one
            method_for_time_index = None
            # - based on the ini/configuration file (if given)
            if 'time_index_method_for_ref_pot_et_netcdf' in list(self.iniItems.meteoOptions.keys()) and\
                                                            self.iniItems.meteoOptions['time_index_method_for_ref_pot_et_netcdf'] != "None":
                method_for_time_index = self.iniItems.meteoOptions['time_index_method_for_ref_pot_et_netcdf']

            if self.refETPotFileNC_set_per_year: 
                nc_file_per_year = self.etpFileNC %(int(currTimeStep.year), int(currTimeStep.year))
                self.referencePotET = vos.netcdf2PCRobjClone(\
                                      nc_file_per_year, self.refETPotVarName,\
                                      str(currTimeStep.fulldate), 
                                      useDoy = method_for_time_index,
                                      cloneMapFileName = self.cloneMap,\
                                      LatitudeLongitude = True)
            else:
                self.referencePotET = vos.netcdf2PCRobjClone(\
                                      self.etpFileNC,self.refETPotVarName,\
                                      str(currTimeStep.fulldate), 
                                      useDoy = method_for_time_index,
                                      cloneMapFileName=self.cloneMap,\
                                      LatitudeLongitude = True)
            #-----------------------------------------------------------------------
            # NOTE: RvB 13/07/2016 added to automatically update reference potential evapotranspiration
            self.referencePotET = self.refETPotConst + self.refETPotFactor * pcr.ifthen(self.landmask, self.referencePotET)
            #-----------------------------------------------------------------------

        
        # extra meteo files/variables (needed for the Penman-Monteith method)
        for meteo_var_name in self.extra_meteo_var_names:  
        #
            if meteo_var_name in list(self.iniItems.meteoOptions.keys()) and self.iniItems.meteoOptions[meteo_var_name].endswith(('.nc', '.nc4', '.nc3')):
                
                # read the file
                method_for_time_index = None
                netcdf_file_name = vos.getFullPath(self.iniItems.meteoOptions[meteo_var_name], self.inputDir)
                vars(self)[meteo_var_name] = vos.netcdf2PCRobjClone(ncFile = netcdf_file_name,\
                                                                    varName = "automatic" ,
                                                                    dateInput = str(currTimeStep.fulldate),\
                                                                    useDoy = method_for_time_index,
                                                                    cloneMapFileName  = self.cloneMap)

                # apply conversion factor and constant
                vars(self)[meteo_var_name] = vars(self)['consta_for_' + meteo_var_name] + \
                                             vars(self)['factor_for_' + meteo_var_name] * pcr.ifthen(self.landmask, vars(self)[meteo_var_name])                                                   
