import pandas as pd
import datetime
from collections import Counter
import requests
from utils import to_date, timeit, debugit, prep_dicts, prep_df, unpack_cols
from exceptions import EmptyDataException, NoAccessException, ServerException, CityAirException
import json
from sys import getsizeof
"""
TODO

docstrings refactor: choose style, added raiseing exception

"""

DEFAULT_HOST = "https://develop.cityair.io/backend-api/request-dev-pg.php?map="


class CityAirRequest:
    """
        object for accessing data of CityAir.io project
        Parameters
        ----------
        user, psw :  str
            authentication information
        host_url: str, default https://develop.cityair.io/backend-api/request-dev-pg.php?map=
            url of the CityAir API, you may want to change it in case using a StandAloneServer
        timeout: int, default 100
            timeout for the server request
    -------"""

    def __init__(self, user, psw, **kwargs):

        self.host_url = kwargs.get('host_url', DEFAULT_HOST)
        self.timeout = kwargs.get('timeout', 100)
        self.user = user
        self.psw = psw

    @debugit
    @timeit
    def _make_request(self, method_url, *keys, **kwargs):
        """
        Making request with the prepared data

        Parameters
        ----------
        method_url :  str
            url of the specified method
        *keys: [str]
            keys, which data to return from the raw server response
        **kwargs : dict
            some additional args to pass to the request
        -------"""
        body = {"User": getattr(self, 'user'), "Pwd": getattr(self, 'psw'), **kwargs}
        url = f"{self.host_url}/{method_url}"
        response = requests.post(url, json=body, timeout=self.timeout)
        if response.json()['IsError']:
            raise ServerException(response)
        response_data = response.json()['Result']
        for key in keys:
            if len(response_data[key]) == 0:
                raise EmptyDataException(response)
        if len(keys) == 0:
            return response_data
        elif len(keys) == 1:
            return response_data[keys[0]]
        else:
            return [response_data[key] for key in keys]

    @timeit
    def get_devices(self, format='list', include_offline=True, include_children=False, **kwargs):
        """
        Provides devices information in various formats

        Parameters
        ----------
        format :  {'list', 'raw', 'dicts}, default 'list'
            in case of 'raw' - returns dataframe including all info got from server
            in case of 'dicts' - returns list of dictionaries, each including keys 'serial_number' and 'name'
        include_offline: bool, default True
            whether to include offline devices to the output
        include_children : bool, default False
            whether to include info of child devices to the output
        timeit: bool, default False
            whether to print how long it took to gather and process data
        debugit: bool, default False
            whether to print raw request and response data
        -------"""
        value_types_data, devices_data = self._make_request(f"DevicesApi2/GetDevices", "PacketsValueTypes", "Devices",
                                                            **kwargs)
        value_types_data = pd.DataFrame.from_records(value_types_data)
        self.value_types = dict(zip(value_types_data['ValueType'], value_types_data['TypeName']))
        df = pd.DataFrame.from_records(devices_data)
        if format == 'raw':
            return df
        df = prep_df(df, dicts_cols=['children'])

        if not include_offline:
            df = df[df['is_online']]
        df_with_children = df.copy()
        for children in df['children']:
            for child in children:
                df_with_children = df_with_children.append(child, ignore_index=True)
        self.device_serials = dict(zip(df_with_children['id'], df_with_children['serial_number']))
        self.device_ids = dict(zip(df_with_children['serial_number'], df_with_children['id']))
        if include_children:
            df = df_with_children
        df.set_index('serial_number', inplace=True, drop=False)

        if format == 'dicts':
            res = []
            for serial in df.index:
                info = dict(df.loc[serial].dropna())
                main_params = ['serial_number', 'name', 'children', 'check_infos']
                single_dict = dict(zip(main_params, [info.pop(param, None) for param in main_params]))
                single_dict['misc'] = sorted(info.items(), key=lambda item: getsizeof(item[1]))
                res.append(sorted(single_dict.items(), key=lambda item: getsizeof(item[1])))
            return res
        elif format == 'list':
            return list(df.index)
        elif format == 'df':
            return df
        else:
            raise ValueError(
                f"Unknown type of format argument: {format}. Available formats are: list, raw, dicts, df")

    @timeit
    def get_device_data(self, serial_number, start_date=None,
                        finish_date=datetime.datetime.now(),
                        take_count=1000, all_cols=False,
                        separate_device_data=False, **kwargs):
        """
        Provides data from the selected device

        Parameters
        ----------
        serial_number : str
            serial_number of the device
        start_date, finish_date: str or datetime.datetime
            dates on which data is being queried
        take_count : int, default 1000
            count of packets which is requested from the server
        all_cols: bool, default False
            whether to keep or drop columns which are not directly related to air
             quality data (i.e. battery status, ps 220, recieve date)
        separate_device_data: bool, default False
            whether to separate dfs for individual devices.
            if False - returns one pd.DataFrame, where value_name is concatenated with
                serial_number of the device if there is more than one device
                measuring values of a type
            if True - returns dictionary, where keys are serial_number of
                the device and value is pd.DataFrame containing all data of each device
        -------"""
        try:
            device_id = self.device_ids[serial_number]
        except AttributeError:
            self.get_devices()
            device_id = self.device_ids[serial_number]
        except KeyError:
            raise NoAccessException(serial_number)
        filter_ = {'Take': take_count,
                   'DeviceId': device_id}
        if start_date:
            filter_['FilterType'] = 1
            filter_['TimeBegin'] = to_date(start_date).isoformat()
            filter_['TimeEnd'] = to_date(finish_date).isoformat()
        else:
            filter_['FilterType'] = 3
            filter_['Skip'] = 0
        packets = self._make_request("DevicesApi2/GetPackets", 'Packets', Filter=filter_, **kwargs)
        df = pd.DataFrame.from_records(packets)
        df.drop(['Data', 'PacketId'], 1, inplace=True, errors='ignore')
        df = unpack_cols(df, 'coordinates')
        # unpacking columns that are list of dict (['Data'])
        records = []
        for packets in df['DataJson']:
            packets = json.loads(packets)
            records.append(dict(zip(
                [f"value {packet['D']} {packet['VT']}" for packet in packets],
                [packet['V'] for packet in packets])))
        df = df.assign(**pd.DataFrame.from_records(records))

        values_cols = list(filter(lambda col: col.startswith('value'), df.columns))
        if separate_device_data:
            res = dict()
            for col in values_cols:
                _, device_id, value_id = col.split(' ')
                serial = self.device_serials[int(device_id)]
                value_name = self.value_types[int(value_id)]
                series_to_append = df[col].rename(value_name)
                try:
                    res[serial] = pd.concat([res[serial], series_to_append], axis=1)
                except KeyError:
                    res[serial] = pd.concat([df['SendDate'], series_to_append], axis=1)
            try:
                res[serial_number] = pd.concat(
                    [df.drop(values_cols + ['DataJson', 'SendDate'], axis=1), res[serial_number]], axis=1)
            except KeyError:
                res[serial_number] = df.drop(values_cols + ['DataJson'], axis=1)
            for device in res:
                res[device] = prep_df(res[device])
                print(res[device].columns)
                res[device].set_index('date', inplace=True, drop=True)
            return res
        else:
            value_types_count = Counter(list(
                map(lambda s: (s.split(' ')[-1]), values_cols)))
            for col in list(filter(lambda col: col.startswith('value'), df.columns)):
                _, device_id, value_id = col.split(' ')
                serial = self.device_serials[int(device_id)]
                value_name = self.value_types[int(value_id)]
                if value_types_count[value_id] > 1:
                    proper_col_name = f"{value_name} [{serial}]"
                else:
                    proper_col_name = f"{value_name}"
                df.rename(columns={col: proper_col_name}, inplace=True)
            df = prep_df(df.drop(['DataJson'], axis=1))
            df.set_index('date', inplace=True, drop=True)
            return df
