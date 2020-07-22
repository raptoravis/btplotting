import time
import logging
from enum import Enum
from threading import Thread, Lock
from tornado import gen

_logger = logging.getLogger(__name__)


class UpdateType(Enum):
    ADD = 1,
    UPDATE = 2,


class LiveDataHandler:

    '''
    Handler for live data
    '''

    def __init__(self, doc, app, figid, lookback, timeout=1):
        # doc of client
        self._doc = doc
        # app instance
        self._app = app
        # figurepage id
        self._figid = figid
        # lookback length
        self._lookback = lookback
        # timeout for thread
        self._timeout = timeout
        # figurepage
        self._figurepage = app.get_figurepage(figid)
        # thread to process new data
        self._thread = Thread(target=self._t_thread, daemon=True)
        self._lock = Lock()
        self._running = True
        self._new_data = False
        self._last_idx = -1
        self._datastore = None
        self._patches = []
        self._cb_patch = None
        self._cb_add = None
        # inital fill of datastore
        self._fill()
        # start thread
        self._thread.start()

    def _fill(self):
        '''
        Fills datastore with latest values
        '''
        with self._lock:
            self._datastore = self._app.generate_data(
                figid=self._figid,
                back=self._lookback,
                preserveidx=True)
            if self._datastore.shape[0] > 0:
                self._last_idx = self._datastore['index'].iloc[-1]
            # init by calling set_cds_columns_from_df
            # after this, all cds will already contain data
            self._figurepage.set_cds_columns_from_df(self._datastore)

    @gen.coroutine
    def _cb_push_adds(self):
        '''
        Streams new data to all ColumnDataSources
        '''
        # take all rows from datastore that were not yet streamed
        update_df = self._datastore[self._datastore['index'] > self._last_idx]
        # skip if we don't have new data
        if update_df.shape[0] == 0:
            return
        # store last index of streamed data
        self._last_idx = update_df['index'].iloc[-1]

        fp = self._figurepage
        # create stream data for figurepage
        data = fp.get_cds_streamdata_from_df(update_df)
        _logger.debug(f'Sending stream for figurepage: {data}')
        fp.cds.stream(
            data, self._get_data_stream_length())

        # create stream df for every figure
        for f in fp.figures:
            data = f.get_cds_streamdata_from_df(update_df)
            _logger.debug(f'Sending stream for figure: {data}')
            f.cds.stream(
                data, self._get_data_stream_length())

    @gen.coroutine
    def _cb_push_patches(self):
        '''
        Pushes patches to all ColumnDataSources
        '''
        # get all rows to patch
        patches = []
        while len(self._patches) > 0:
            patches.append(self._patches.pop(0))
        # skip if no patches available
        if len(patches) == 0:
            return

        for patch in patches:
            fp = self._figurepage

            # patch figurepage
            p_data, s_data = fp.get_cds_patchdata_from_series(patch)
            if len(p_data) > 0:
                _logger.debug(f'Sending patch for figurepage: {p_data}')
                fp.cds.patch(p_data)
            if len(s_data) > 0:
                _logger.debug(f'Sending stream for figurepage: {s_data}')
                fp.cds.stream(
                    s_data, self._get_data_stream_length())

            # patch all figures
            for f in fp.figures:
                p_data, s_data = f.get_cds_patchdata_from_series(patch)
                if len(p_data) > 0:
                    _logger.debug(f'Sending patch for figure: {p_data}')
                    f.cds.patch(p_data)
                if len(s_data) > 0:
                    _logger.debug(f'Sending stream for figure: {s_data}')
                    f.cds.stream(
                        s_data, self._get_data_stream_length())

    def _push_adds(self):
        doc = self._doc
        try:
            doc.remove_next_tick_callback(self._cb_add)
        except ValueError:
            pass
        self._cb_add = doc.add_next_tick_callback(
            self._cb_push_adds)

    def _push_patches(self):
        doc = self._doc
        try:
            doc.remove_next_tick_callback(self._cb_patch)
        except ValueError:
            pass
        self._cb_patch = doc.add_next_tick_callback(
            self._cb_push_patches)

    def _process(self, rows):
        '''
        Request to update data with given rows
        '''
        for idx, row in rows.iterrows():
            if (self._datastore.shape[0] > 0
                    and idx in self._datastore['index']):
                update_type = UpdateType.UPDATE
            else:
                update_type = UpdateType.ADD

            if update_type == UpdateType.UPDATE:
                ds_idx = self._datastore.loc[
                    self._datastore['index'] == idx].index[0]
                with self._lock:
                    self._datastore.at[ds_idx] = row
                self._patches.append(row)
                self._push_patches()
            else:
                # append data and remove old data
                with self._lock:
                    self._datastore = self._datastore.append(row)
                    self._datastore = self._datastore.tail(
                        self._get_data_stream_length())
                self._push_adds()

    def _t_thread(self):
        '''
        Thread method for datahandler
        '''
        while self._running:
            if self._new_data:
                data = self._app.generate_data(
                    start=self._last_idx,
                    preserveidx=True)
                self._new_data = False
                self._process(data)
            time.sleep(self._timeout)

    def _get_data_stream_length(self):
        '''
        Returns the length of data stream to use
        '''
        return min(self._lookback, self._datastore.shape[0])

    def get_last_idx(self):
        '''
        Returns the last index in local datastore
        '''
        if self._datastore.shape[0] > 0:
            return self._datastore['index'].iloc[-1]
        return -1

    def set(self, df):
        '''
        Sets a new df and streams data
        '''
        with self._lock:
            self._datastore = df
            self._last_idx = -1
        self._push_adds()

    def update(self):
        '''
        Notifies datahandler of new data
        '''
        self._new_data = True

    def stop(self):
        '''
        Stops the datahandler
        '''
        self._running = False
        # it would not really be neccessary to join this thread but doing
        # it for readability
        self._thread.join(0)
