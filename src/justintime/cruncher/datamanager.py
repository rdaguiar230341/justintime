
# from . watcher import Watcher
import os.path
import fnmatch
from os import walk
import re

# DUNE DAQ includes
import daqdataformats
# import detdataformats.wib
# import detdataformats.wib2
# import detdataformats.trigger_primitive
import detchannelmaps
import hdf5libs
import rawdatautils.unpack.wib as protowib_unpack
import rawdatautils.unpack.wib2 as wib_unpack
import logging

import numpy as np
import pandas as pd
import collections
import rich
from rich import print
from itertools import groupby
from collections import defaultdict


from ..utils import rawdataunpacker as rdu



# from . import unpack_fwtps

"""
DataManager is responsible of raw data information management: discovery, loading, and reference runs handling

"""

# def get_protowib_header_info( frag ):
#         wf = detdataformats.wib.WIBFrame(frag.get_data())
#         wh = wf.get_wib_header()

#         logging.debug(f"detector_id {0}, crate: {wh.crate_no}, slot: {wh.slot_no}, fibre: {wh.fiber_no}")

#         return (0, wh.crate_no, wh.slot_no, wh.fiber_no)

# def get_wib_header_info( frag ):
#     wf = detdataformats.wib2.WIB2Frame(frag.get_data())
#     wh = wf.get_header()
#     logging.debug(f"detector {wh.detector_id}, crate: {wh.crate}, slot: {wh.slot}, fibre: {wh.link}")

#     return (wh.detector_id, wh.crate, wh.slot, wh.link)

# # WARNING: Duplicate from DataManager: factorize!
# def fwtp_list_to_df(fwtps: list, ch_map):
#     fwtp_array = []

#     for fwtp in fwtps:
#         tph = fwtp.get_header()
#         tpt = fwtp.get_trailer()

#         for j in range(fwtp.get_n_hits()):
#             tpd = fwtp.get_data(j)
#             fwtp_array.append((
#                 tph.get_timestamp(),
#                 ch_map.get_offline_channel_from_crate_slot_fiber_chan(tph.crate_no, tph.slot_no, tph.fiber_no, tph.wire_no),
#                 tph.crate_no, 
#                 tph.slot_no,
#                 tph.fiber_no,
#                 tph.wire_no,
#                 tph.flags,
#                 tpt.median,
#                 tpt.accumulator,
#                 tpd.start_time,
#                 tpd.end_time,
#                 tpd.peak_time,
#                 tpd.peak_adc,
#                 tpd.hit_continue,
#                 tpd.tp_flags,
#                 tpd.sum_adc
#             ))
#     #rprint(f"Unpacked {len(fwtp_array)} FW TPs")

#     rtp_df = pd.DataFrame(fwtp_array, columns=['ts', 'offline_ch', 'crate_no', 'slot_no', 'fiber_no', 'wire_no', 'flags', 'median', 'accumulator', 'start_time', 'end_time', 'peak_time', 'peak_adc', 'hit_continue', 'tp_flags', 'sum_adc'])

#     return rtp_df


class VSTChannelMap(object):

    @staticmethod
    def get_offline_channel_from_crate_slot_fiber_chan(crate_no, slot_no, fiber_no, ch_no):
        return 256*fiber_no+ch_no

    @staticmethod
    def get_plane_from_offline_channel(ch):
        return 0


class DataManager:

    # match_exprs = ['*.hdf5','*.hdf5.copied']
    match_exprs = ['*.hdf5', '*.hdf5.copied']
    max_cache_size = 100
    # frametype_map = {
    #     'ProtoWIB': (get_protowib_header_info, protowib_unpack, daqdataformats.FragmentType.kProtoWIB),
    #     'WIB': (get_wib_header_info, wib_unpack, daqdataformats.FragmentType.kWIB),
    # }

    @staticmethod 
    def make_channel_map(map_name):


        match map_name+'ChannelMap':
            case 'VDColdboxChannelMap':
                return detchannelmaps.make_map('VDColdboxChannelMap')
            # case 'ProtoDUNESP1ChannelMap':
            #     return detchannelmaps.make_map('ProtoDUNESP1ChannelMap')
            case 'PD2HDChannelMap':
                return detchannelmaps.make_map('PD2HDChannelMap')
            case 'HDColdboxChannelMap':
                return detchannelmaps.make_map('HDColdboxChannelMap')
            case 'VSTChannelMap':
                return VSTChannelMap()
            case _:
                raise RuntimeError(f"Unknown channel map id '{map_name}'")


    def __init__(self, data_path: str, channel_map_name: str = 'PDHD') -> None:

        if not os.path.isdir(data_path):
            raise ValueError(f"Directory {data_path} does not exist")

        self.data_path = data_path
        self.ch_map_name = channel_map_name
        self.ch_map = self.make_channel_map(channel_map_name) 

        self.offch_to_hw_map = self._init_o2h_map()
        self.femb_to_offch = {k: [int(x) for x in d] for k, d in groupby(self.offch_to_hw_map, self.femb_id_from_offch)}

        self.cache = collections.OrderedDict()

    def _init_o2h_map(self):
        if self.ch_map_name == 'VDColdbox':
            crate_no = 4
            slots = range(4)
            fibres = range(1, 3)
            chans = range(256)
        else:
            return {}

        o2h_map = {}
        for slot_no in slots:
            for fiber_no in fibres:
                for ch_no in chans:
                    off_ch = self.ch_map.get_offline_channel_from_crate_slot_fiber_chan(crate_no, slot_no, fiber_no, ch_no)
                    if off_ch == 4294967295:
                        continue
                    o2h_map[off_ch] = (crate_no, slot_no, fiber_no, ch_no)

        return o2h_map

    def femb_id_from_offch(self, off_ch):
        # off_ch_str = str(off_ch)
        crate, slot, link, ch = self.offch_to_hw_map[off_ch]
        return (4*slot+2*(link-1)+ch//128)+1 


    def list_files(self) -> list:
        files = []
        for m in self.match_exprs:
            files += fnmatch.filter(next(walk(self.data_path), (None, None, []))[2], m)  # [] if no file

        return sorted(files, reverse=True, key=lambda f: os.path.getmtime(os.path.join(self.data_path, f)))


    def get_session_run_files_map(self) -> defaultdict:
        re_app_run = re.compile(r'(.*)_run(\d*)')

        # List files
        lf = self.list_files()

        def extract_session_run( fname : str ):
            m = re_app_run.match(fname)
            if not m:
                return ('none', '0')
            else:
                return m.groups()

        # Group by regex
        gf_it = groupby(lf, extract_session_run)
        gf = {k: [x for x in d if x] for k,d in gf_it}
        srf_map = defaultdict(dict)

        # Populate the map
        for k,v in gf.items():
            srf_map[k[0]][int(k[1])]=v

        return srf_map

    def has_trigger_records(self, file_name: str) -> list:
        file_path = os.path.join(self.data_path, file_name)
        rdf = hdf5libs.HDF5RawDataFile(file_path) # number of events = 10000 is not used
        try:
            _ = rdf.get_all_trigger_record_ids()
            return True
        except RuntimeError:
            return False

    def has_timeslices(self, file_name: str) -> list:
        file_path = os.path.join(self.data_path, file_name)
        rdf = hdf5libs.HDF5RawDataFile(file_path) # number of events = 10000 is not used
        try:
            return [ n for n,_ in rdf.get_all_timeslice_ids()]
        except RuntimeError:
            return []

    def get_trigger_record_list(self, file_name: str) -> list:
        file_path = os.path.join(self.data_path, file_name)
        rdf = hdf5libs.HDF5RawDataFile(file_path) # number of events = 10000 is not used
        try:
            return [ n for n,_ in rdf.get_all_trigger_record_ids()]
        except RuntimeError:
            return []

    def get_timeslice_list(self, file_name: str) -> list:
        file_path = os.path.join(self.data_path, file_name)
        rdf = hdf5libs.HDF5RawDataFile(file_path) # number of events = 10000 is not used
        try:
            return [ n for n,_ in rdf.get_all_timeslice_ids()]
        except RuntimeError:
            return []


    def get_entry_list(self, file_name: str) -> list:
        trl = self.get_trigger_record_list(file_name)
        tsl = self.get_timeslice_list(file_name)

        return trl if trl else tsl


    def load_entry(self, file_name: str, entry: int) -> list:
        uid = (file_name, entry)
        if uid in self.cache:
            logging.info(f"{file_name}:{entry} already loaded. returning cached dataframe")
            en_info, tpc_df, tp_df, fwtp_df = self.cache[uid]
            self.cache.move_to_end(uid, False)
            return en_info, tpc_df, tp_df, fwtp_df

        file_path = os.path.join(self.data_path, file_name)
        rdf = hdf5libs.HDF5RawDataFile(file_path) # number of events = 10000 is not used
            
        # Check if we're dealing with tr ot ts
        has_trs = False
        try:
            _ = rdf.get_all_trigger_record_ids()
            has_trs = True
        except:
            pass

        has_tss = False
        try:
            _ = rdf.get_all_timeslice_ids()
            has_tss = True
        except:
            pass

        #----

        if has_trs:
            logging.debug(f"Trigger Records detected!")
            get_entry_hdr = rdf.get_trh
        elif has_tss:
            logging.debug(f"TimeSlices detected!")
            get_entry_hdr = rdf.get_tsh

        else:
            raise RuntimeError(f"No TriggerRecords nor TimeSlices found in {file_name}")

        en_hdr = get_entry_hdr((entry,0))
        # en_source_ids = rdf.get_source_ids((entry, 0))

        if has_trs:
            en_info = {
                'run_number': en_hdr.get_run_number(),
                'trigger_number': en_hdr.get_trigger_number(),
                'trigger_timestamp': en_hdr.get_trigger_timestamp(),
            }
            en_ts = en_hdr.get_trigger_timestamp()

        elif has_tss:
            en_info = {
                'run_number': en_hdr.run_number,
                'trigger_number': en_hdr.timeslice_number,
                'trigger_timestamp': 0,
            }
            en_ts = 0

        logging.info(en_info)

        wf_up = rdu.WIBFragmentUnpacker(self.ch_map_name)
        wethf_up = rdu.WIBEthFragmentPandasUnpacker(self.ch_map_name)
        tp_up = rdu.TPFragmentPandasUnpacker(self.ch_map_name)
        logging.debug("Upackers created")

        up = rdu.UnpackerService()
        logging.debug("UnpackerService created")

        up.add_unpacker('bde_eth', wethf_up)
        up.add_unpacker('bde_flx', wf_up)
        up.add_unpacker('tp', tp_up)
        # up.add_unpacker('pds', daphne_up)
        logging.debug("Upackers added")
        print("aaa 3")

        unpacked_tr = up.unpack(rdf, entry)
        logging.info("Unpacking completed")

        # Assembling BDE dataframes
        tpc_dfs = {}
        fwtp_df = pd.DataFrame( columns=['ts'])
        tp_df = pd.DataFrame( columns=['ts'])

        if 'bde_eth' in unpacked_tr:
            dfs = {k:v for k,v in unpacked_tr['bde_eth'].items() if not v is None}
            print(f"Collected {len(dfs)} non-empty DUNEWIBEth Frames")
            tpc_dfs.update(dfs)
        if 'bde_flx' in unpacked_tr:
            dfs = {k:v for k,v in unpacked_tr['bde_flx'].items() if not v is None}
            print(f"Collected {len(dfs)} non-empty DUNEWIB Frames")
            tpc_dfs.update(dfs)

        idx = pd.Index([], dtype='uint64')
        for df in tpc_dfs.values():
            idx = idx.union(df.index)

        tpc_df = pd.DataFrame(index=idx, dtype='uint16')
        for df in tpc_dfs.values():
            tpc_df = tpc_df.join(df)
        tpc_df = tpc_df.reindex(sorted(tpc_df.columns), axis=1)

        print(f"TPC-BDE adcs dataframe assembled {len(tpc_df)} samples x {len(tpc_df.columns)} chans from sources {list(tpc_dfs)}")

        # Assembling TPC-TP dataframes
        if 'tp' in unpacked_tr:
            print("Assembling TPs")
            tp_df = pd.concat(unpacked_tr['tp'].values())
            tp_df = tp_df.sort_values(by=['time_start', 'channel'])
            print(f"TPs dataframe assembled {len(tp_df)}")



        self.cache[uid] = (en_info, tpc_df, tp_df, fwtp_df)
        if len(self.cache) > self.max_cache_size:
            old_uid, _ = self.cache.popitem(False)
            logging.info(f"Removing {old_uid[0]}:{old_uid[1]} from cache")

        return en_info, tpc_df, tp_df, fwtp_df

