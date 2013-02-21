# This file is part of PyTradeLib.
#
# Copyright 2013 Brian A Cappello <briancappello at gmail>
#
# PyTradeLib is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PyTradeLib is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with PyTradeLib.  If not, see http://www.gnu.org/licenses/

import os
import lz4
import gzip

import matplotlib.mlab as mlab

from pytradelib import bar
from pytradelib import utils
from pytradelib import observer
from pytradelib import settings
from pytradelib.data.providers import ProviderFactory
from pytradelib.data.failed import Symbols as FailedSymbols


'''
The historical parsing code is implemented as a pluggable generator pipeline:
[See Reader.__get_bars() and Updater.__update_symbols() for the code.
Bookmark that caused this experiment: http://www.dabeaz.com/generators-uk/]

    Initialize the generator pipeline with an Instrument or [Instruments]
        |
        V
 file_path(s) -> file_open -> file_to_rows_reader -> row_filter -> parser/drain
                                                                        |
    Return values from Reader.get_X_bars():                             V
                              for Instrument --------------> [list of bar.Bar]
                              for Instruments --> {"symbol": [list of bar.Bar]}
'''

def __yield_open_files(tag_file_paths, mode):
    '''
    :param tag_file_paths: tuple(anything, file_path_to_open)
    :param mode: any mode supported by the selected compression backend
    '''
    for tag, file_path in tag_file_paths:
        if mode == 'w':
            utils.mkdir_p(os.path.dirname(file_path))
        compression = settings.DATA_COMPRESSION
        if compression == 'gz':
            f = gzip.open(file_path, mode)
        elif not compression or compression == 'lz4':
            f = open(file_path, mode)
        yield (tag, f)

def open_files_readable(symbol_file_paths):
    for symbol_file_handle in __yield_open_files(symbol_file_paths, 'r'):
        yield symbol_file_handle

def open_files_writeable(data_file_paths):
    for data_file_handle in __yield_open_files(data_file_paths, 'w'):
        yield data_file_handle

def open_files_updatable(data_file_paths):
    for data_file_handle in __yield_open_files(data_file_paths, 'r+'):
        yield data_file_handle


def symbol_rows(symbol_files):
    for symbol, f in symbol_files:
        data = f.read()
        f.close()
        if settings.DATA_COMPRESSION == 'lz4':
            data = lz4.loads(data)

        # split the file into rows, slicing off the header labels
        csv_rows = data.strip().split('\n')[1:]
        yield (symbol, csv_rows)


# FIXME: For the next three functions, we still read the entire file. How much
# is gained by reading only the first/last few lines of the file?

def newest_and_oldest_symbol_rows(symbol_files):
    symbol_rows_ = symbol_rows(symbol_files)
    for symbol, rows in symbol_rows_:
        yield (symbol, [rows[-1], rows[0]])

# FIXME: For the next two functions, optionally return count bars from beg/end?

# oldest date (assumed to be the IPO date)
def oldest_symbol_row(symbol_files):
    symbol_rows_ = symbol_rows(symbol_files)
    for symbol, rows in symbol_rows_:
        yield (symbol, [rows[0]])

# most recent date
def newest_symbol_row(symbol_files):
    symbol_rows_ = symbol_rows(symbol_files)
    for symbol, rows in symbol_rows_:
        yield (symbol, [rows[-1]])


class Reader(object):
    def __init__(self):
        self.set_data_provider(settings.DATA_STORE_FORMAT)

    def set_data_provider(self, data_provider, default_frequency=None):
        self._default_frequency = default_frequency or bar.Frequency.DAY
        self._data_reader = ProviderFactory.get_data_provider(data_provider)

    def set_bar_filter(self, bar_filter):
        self._data_reader.set_bar_filter(bar_filter)

    def get_recarray(self, symbol, frequency=None):
        return self.get_recarrays([symbol], frequency)[0]
        
    def get_recarrays(self, symbols, frequency=None):
        frequency = frequency or self._default_frequency

        # define the pipeline
        symbol_file_handles =  open_files_readable(
            self._data_reader.get_symbol_file_paths(symbols, frequency) )

        # start and drain the pipeline
        ret = []
        for symbol, f in symbol_file_handles:
            recarray = mlab.csv2rec(f)
            recarray.sort()
            ret.append(recarray)
        return ret

    def get_bars(self, symbol, frequency=None):
        ret = self.__get_bars(
            [symbol], symbol_rows, frequency, use_bar_filter=True)
        return ret[symbol] # return just the list of bars for the symbol

    def get_bars_dict(self, symbols, frequency=None):
        return self.__get_bars(
            symbols, symbol_rows, frequency, use_bar_filter=True)

    # FIXME: are all the following public functions *really* needed?
    def get_newest_bar(self, symbol, frequency=None):
        ret = self.__get_bars(
            [symbol], newest_symbol_row, frequency, use_bar_filter=False)
        return ret[symbol][0] # return just the first bar for the symbol

    def get_newest_bars_dict(self, symbols, frequency=None):
        ret = self.__get_bars(
            symbols, newest_symbol_row, frequency, use_bar_filter=False)
        for symbol, bars in ret.items():
            ret[symbol] = bars[0] # return just the first bar for the symbols
        return ret

    def get_oldest_bar(self, symbol, frequency=None):
        ret = self.__get_bars(
            [symbol], oldest_symbol_row, frequency, use_bar_filter=False)
        return ret[symbol][0] # return just the last bar for the symbol

    def get_oldest_bars_dict(self, symbols, frequency=None):
        ret = self.__get_bars(
            symbols, oldest_symbol_row, frequency, use_bar_filter=False)
        for symbol, bars in ret.items():
            ret[symbol] = bars[0] # return just the last bar for the symbols
        return ret

    def get_newest_and_oldest_bars(self, symbol, frequency=None):
        ret = self.__get_bars([symbol], newest_and_oldest_symbol_rows,
                               frequency, use_bar_filter=False)
        return ret[symbol] # return a list [first_bar, last_bar] for the symbol

    def get_newest_and_oldest_bars_dict(self, symbols, frequency=None):
        return self.__get_bars(symbols, newest_and_oldest_symbol_rows,
                               frequency, use_bar_filter=False)

    def __get_bars(self, symbols, row_generator, frequency, use_bar_filter):
        frequency = frequency or self._default_frequency

        # define the pipeline
        symbol_rows_ = row_generator( open_files_readable(
            self._data_reader.get_symbol_file_paths(symbols, frequency)) )

        # start the pipeline and and drain the results into ret
        ret = {}
        for symbol, rows in symbol_rows_:
            symbol, bars = self._data_reader.rows_to_bars(
                symbol, rows, frequency, use_bar_filter)
            if bars:
                ret[symbol] = bars
        return ret


def process_data_to_initialize(data_files, provider):
    for rows, f in data_files:
        frequency = utils.frequency_from_file_path(f.name)
        rows.insert(0, provider.get_csv_column_labels(frequency))
        yield (rows, f)


def process_data_to_convert(data_file_paths, from_provider, to_provider):
    for rows, file_path in data_file_paths:
        symbol = utils.symbol_from_file_path(file_path)
        frequency = utils.frequency_from_file_path(file_path)
        new_file_path = to_provider.get_file_path(symbol, frequency)
        header = rows.pop(0)
        symbol, bars = from_provider.rows_to_bars(symbol, rows, frequency)
        symbol, new_rows = to_provider.bars_to_rows(symbol, bars, frequency)
        new_rows.insert(0, to_provider.get_csv_column_labels(frequency))
        yield (formatted_rows, new_file_path)


def process_data_to_update(data_files, provider):
    for update_rows, f in data_files:
        # read existing data, relying on string sorting for date comparisons
        if utils.supports_seeking(settings.DATA_COMPRESSION):
            # read the tail of the file to rows and get newest stored datetime
            new_rows = []
            try: f.seek(-512, 2)
            except IOError: f.seek(0)
            newest_existing_datetime = f.read().split('\n')[-1].split(',')[0]

        elif settings.DATA_COMPRESSION == 'lz4':
            # read entire file to rows and get newest stored datetime
            new_rows = lz4.loads(f.read()).strip().split('\n')
            newest_existing_datetime = new_rows[-1].split(',')[0]

        # only add new rows if row datetime is greater than stored datetime
        for row in update_rows:
            row_datetime = row.split(',')[0]
            if row_datetime > newest_existing_datetime:
                new_rows.append(row)

        # seek to the proper place in the file in preparation for write_data
        if utils.supports_seeking(settings.DATA_COMPRESSION):
            # jump to the end of the file so we only update existing data
            try: f.seek(-1, 2)
            except IOError: print 'unexpected file seeking bug :(', f.name
            # make sure there's a trailing new-line character at the end
            last_char = f.read()
            if last_char != '\n':
                f.write('\n')
        elif settings.DATA_COMPRESSION == 'lz4':
            # jump to the beginning of the file so we rewrite everything
            f.seek(0)

        yield (new_rows, f)


def write_data(data_files, provider):
    for rows, f in data_files:
        if rows:
            symbol = utils.symbol_from_file_path(f.name)
            frequency = utils.frequency_from_file_path(f.name)
            latest_date_time = \
                provider.row_to_bar(rows[-1], frequency).get_date_time()
            data = '\n'.join(rows)
            if settings.DATA_COMPRESSION == 'lz4':
                data = lz4.dumps(data)
            f.write(data)
            f.close()
            yield (symbol, latest_date_time)
        else:
            file_path = f.name
            f.close()
            if os.stat(file_path).st_size == 0:
                os.remove(file_path)
            continue


class Updater(object):
    def __init__(self, db):
        self._updated_event = observer.Event()
        self._db = db
        self.set_provider_formats(settings.DATA_PROVIDER,
                                  settings.DATA_STORE_FORMAT)

    def set_provider_formats(self, downloader, writer, default_frequency=None):
        self._downloader_format = downloader.lower()
        self._writer_format = writer.lower()
        self._default_frequency = default_frequency or bar.Frequency.DAY

        self._data_downloader = ProviderFactory.get_data_provider(
                                                    self._downloader_format)
        self._data_writer = ProviderFactory.get_data_provider(
                                                    self._writer_format)

    def get_update_event_handler(self):
        return self._updated_event

    def initialize_symbol(self, symbol, frequency=None):
        self.initialize_symbols([symbol], frequency)

    def initialize_symbols(self, symbols, frequency=None):
        frequency = frequency or self._default_frequency
        initialized = [x for x in symbols
                       if self._data_writer.symbol_initialized(x, frequency)\
                       or x in FailedSymbols]
        if initialized:
            print '%i symbols %s already initialized!' % (
                len(initialized), initialized)
            for symbol in initialized:
                symbols.pop(symbols.index(symbol))
        if not symbols:
            print 'no symbols to initialize.'
            return None
        display_progress = True if len(symbols) > 1 else False

        for symbol, latest_dt in self.__update_symbols(symbols, frequency,
            display_progress=display_progress,
            sleep=1
        ):
            self._updated_event.emit(symbol, frequency, latest_dt)

    def update_symbol(self, symbol, frequency=None):
        self.update_symbols([symbol], frequency)

    def update_symbols(self, symbols, frequency=None):
        frequency = frequency or self._default_frequency
        uninitialized = \
            [x for x in symbols
             if x not in FailedSymbols \
             and not self._data_writer.symbol_initialized(x, frequency)]
        if uninitialized:
            print '%i symbols %s not initialized yet!' % (
                len(uninitialized), uninitialized)
            for symbol in uninitialized:
                symbols.pop(symbols.index(symbol))
            if not symbols:
                return None
        display_progress = True if len(symbols) > 1 else False

        for symbol, latest_dt in self.__update_symbols(symbols, frequency,
            operation_name='update',
            display_progress=display_progress,
            open_files_function=open_files_updatable,
            process_data_update_function=process_data_to_update,
            init=False,
            sleep=1
        ):
            self._updated_event.emit(symbol, frequency, latest_dt)

    def __update_symbols(self, symbols, frequency,
        operation_name='download',
        display_progress=False,
        open_files_function=open_files_writeable,
        process_data_update_function=process_data_to_initialize,
        init=True,
        sleep=None
    ):
        '''
        This function contains the actual pipeline logic for downloading,
        initializing and updating symbols' data. It can display the rough
        progress of bulk operation to stdout using display_progress.
        '''
        frequency = frequency or self._default_frequency
        batch_size = 250 if frequency is not bar.Frequency.MINUTE else 500
        sleep = sleep if frequency is not bar.Frequency.MINUTE else None

        # Load the latest stored datetime for the requested combination of
        # symbols and frequency. This doubles as a flag for init vs update.\
        symbol_times = dict((x, None) for x in symbols)
        if frequency != bar.Frequency.MINUTE and not init:
            symbol_times = dict(
                (x, self._db.get_updated(bar.FrequencyToStr[frequency], x))
                for x in symbol_times)
        elif not init:
            symbol_times = dict((x, 1) for x in symbol_times)

        url_file_paths = [x for x in self._data_downloader.get_url_file_paths(
                          symbol_times, frequency)]
        if not url_file_paths:
            op = ' ' if not display_progress else ' bulk '
            raise Exception('no urls returned for%s%sing historical data!' % (
                                                 op, operation_name))
        elif display_progress:
            total_len = len(url_file_paths)
            current_idx = 0
            last_pct = 0
            print 'starting bulk %s of historical data for %i symbols.' % (
                                 operation_name, total_len)

        # download, process and update/save
        for urls in utils.batch(url_file_paths, size=batch_size, sleep=sleep):
            # pipeline for downloading data and preprocessing it
            data_file_paths = self._data_downloader.process_downloaded_data(
                self._data_downloader.verify_downloaded_data(
                    utils.bulk_download(urls)), frequency)

            # if necessary, convert downloaded format into a new storage format
            if self._downloader_format != self._writer_format:
                data_file_paths = process_data_to_convert(data_file_paths,
                    self._data_downloader, self._data_writer)

            # pipeline for opening files and saving/updating downloaded data
            for symbol, latest_date_time in write_data(
                process_data_update_function(
                    open_files_function(data_file_paths),
                    self._data_writer
                    ),
                self._data_writer
            ):
                if display_progress:
                    current_idx += 1
                    pct = int(current_idx / (total_len + 1.0) * 100.0)
                    if pct != last_pct:
                        last_pct = pct
                        print '%i%%' % pct

                yield (symbol, latest_date_time)

        if display_progress:
            if last_pct != 100:
                print '100%'

        yield (None, None) # poison pill to signal end of downloads
