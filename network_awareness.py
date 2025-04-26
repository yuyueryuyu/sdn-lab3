from os_ken.base import app_manager
from os_ken.base.app_manager import lookup_service_brick
from os_ken.ofproto import ofproto_v1_3
from os_ken.controller.handler import set_ev_cls
from os_ken.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, DEAD_DISPATCHER, HANDSHAKE_DISPATCHER
from os_ken.controller import ofp_event
from os_ken.lib.packet import packet
from os_ken.lib.packet import ethernet, arp
from os_ken.lib import hub
from os_ken.topology import event
from os_ken.topology.api import get_host, get_link, get_switch
from os_ken.topology.switches import LLDPPacket

import networkx as nx
import time


GET_TOPOLOGY_INTERVAL = 2
SEND_ECHO_REQUEST_INTERVAL = .05
GET_DELAY_INTERVAL = 2


class NetworkAwareness(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(NetworkAwareness, self).__init__(*args, **kwargs)
        self.switch_info = {}  # dpid: datapath
        self.link_info = {}  # (s1, s2): s1.port
        self.port_link={} # s1,port:s1,s2
        self.port_info = {}  # dpid: (ports linked hosts)
        self.topo_map = nx.Graph()
        self.topo_thread = hub.spawn(self._get_topology)
        self.echo_thread = hub.spawn(self._get_delay)
        self.lldp_delay = {}
        self.echo_delay = {}
        self.switches = None
        self.weight = 'hop'


    def add_flow(self, datapath, priority, match, actions):
        dp = datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=priority, match=match, instructions=inst)
        dp.send_msg(mod)

    def send_echo_request(self, datapath):
        ofp_parser = datapath.ofproto_parser
        now = time.time()
        req = ofp_parser.OFPEchoRequest(datapath, str(now).encode())
        datapath.send_msg(req)
    
    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def echo_reply_handler(self, ev):
        now = time.time()
        msg = ev.msg
        dpid = msg.datapath.id
        start = float(msg.data.decode())
        self.echo_delay[dpid] = max(now - start, 0)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, match, actions)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        dpid = dp.id

        if ev.state == MAIN_DISPATCHER:
            self.switch_info[dpid] = dp

        if ev.state == DEAD_DISPATCHER and dpid in self.switch_info:
            del self.switch_info[dpid]

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_hander(self, ev):
        msg = ev.msg
        dpid = msg.datapath.id
        try:
            src_dpid, src_port_no = LLDPPacket.lldp_parse(msg.data)
            if self.switches is None:
                self.switches = lookup_service_brick('switches')
            for port in self.switches.ports.keys():
                if src_dpid == port.dpid and src_port_no == port.port_no:
                    self.lldp_delay[src_dpid, dpid] = max(self.switches.ports[port].delay, 0)
        except:
            return
    
    def _get_delay(self):
        '''
        发送echo请求，并获取链路delay
        '''
        while True:
            # 遍历交换机，发送echo request
            for dp in self.switch_info.values():
                self.send_echo_request(dp)
                # 每发送一个echo request暂停一段时间
                hub.sleep(SEND_ECHO_REQUEST_INTERVAL)
            
            # 遍历链路的每条边
            for src, dst in self.topo_map.edges:
                # 如果是包括主机的链路，跳过。
                if self.topo_map[src][dst]['is_host']:
                    continue
                try:
                    # 按照公式计算链路时延。
                    lldp_delay_s12 = self.lldp_delay[src, dst]
                    lldp_delay_s21 = self.lldp_delay[dst, src]
                    echo_delay_s1 = self.echo_delay[src]
                    echo_delay_s2 = self.echo_delay[dst]
                    delay = (lldp_delay_s12 + lldp_delay_s21 - echo_delay_s1 - echo_delay_s2) / 2
                    delay = max(delay, 0)
                    self.topo_map[src][dst]['delay'] = delay
                except:
                    continue
            if self.weight == 'delay':
                self.show_topo_map()
            hub.sleep(GET_DELAY_INTERVAL)

    def _get_topology(self):
        _hosts, _switches, _links = None, None, None
        while True:
            hosts = get_host(self)
            switches = get_switch(self)
            links = get_link(self)

            # update topo_map when topology change
            if [str(x) for x in hosts] == _hosts and [str(x) for x in switches] == _switches and [str(x) for x in links] == _links:
                # 在continue的时候也要暂停一段时间。
                hub.sleep(GET_TOPOLOGY_INTERVAL)
                continue
            _hosts, _switches, _links = [str(x) for x in hosts], [str(x) for x in switches], [str(x) for x in links]

            for switch in switches:
                self.port_info.setdefault(switch.dp.id, set())
                # record all ports
                for port in switch.ports:
                    self.port_info[switch.dp.id].add(port.port_no)

            for host in hosts:
                # take one ipv4 address as host id
                if host.ipv4:
                    self.link_info[(host.port.dpid, host.ipv4[0])] = host.port.port_no
                    self.topo_map.add_edge(host.ipv4[0], host.port.dpid, hop=1, delay=0, is_host=True)
            for link in links:
                # delete ports linked switches
                self.port_info[link.src.dpid].discard(link.src.port_no)
                self.port_info[link.dst.dpid].discard(link.dst.port_no)

                # s1 -> s2: s1.port, s2 -> s1: s2.port
                self.port_link[(link.src.dpid,link.src.port_no)]=(link.src.dpid, link.dst.dpid)
                self.port_link[(link.dst.dpid,link.dst.port_no)] = (link.dst.dpid, link.src.dpid)

                self.link_info[(link.src.dpid, link.dst.dpid)] = link.src.port_no
                self.link_info[(link.dst.dpid, link.src.dpid)] = link.dst.port_no
                

                self.topo_map.add_edge(link.src.dpid, link.dst.dpid, hop=1, is_host=False)

            if self.weight == 'hop':
                self.show_topo_map()
            hub.sleep(GET_TOPOLOGY_INTERVAL)
    
    # network_awareness类的port_status_handler，在控制器主类的方法中调用
    def port_status_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        port = msg.desc.port_no
        reason = msg.reason
        ofproto = dp.ofproto
        
        # 如果是端口失效
        if reason == ofproto.OFPPR_DELETE or (reason == ofproto.OFPPR_MODIFY and msg.desc.state in [ofproto.OFPPS_LINK_DOWN, ofproto.OFPPS_BLOCKED]):

            # 在link_info中删除对应端口的链路
            to_discard = []
            for s1, s2 in self.link_info:
                if s1 == dp.id and self.link_info[s1, s2] == port:
                    to_discard.append((s1, s2))
            for key in to_discard:
                self.link_info.pop(key, None)
            
            # 在port_link, topo_map中删除包含对应端口的链路
            to_discard = []
            for s1, p in self.port_link:
                _, s2 = self.port_link[s1, p]
                if s1 == dp.id and p == port:
                    if self.topo_map.has_edge(s1, s2):
                        self.topo_map.remove_edge(s1, s2)
                    self.lldp_delay.pop((s1, s2), None)
                    self.lldp_delay.pop((s2, s1), None)
                    to_discard.append((s1, port))
            for key in to_discard:
                self.port_link.pop(key, None)
            
            # 在port_info中删除对应链路
            self.port_info[dp.id].discard(port)
        
        # 如果是新增端口
        else:
            # 重新发现链路
            hosts = get_host(self)
            switches = get_switch(self)
            links = get_link(self)
            for switch in switches:
                self.port_info.setdefault(switch.dp.id, set())
                # record all ports
                for port in switch.ports:
                    self.port_info[switch.dp.id].add(port.port_no)

            for host in hosts:
                # take one ipv4 address as host id
                if host.ipv4:
                    self.link_info[(host.port.dpid, host.ipv4[0])] = host.port.port_no
                    self.topo_map.add_edge(host.ipv4[0], host.port.dpid, hop=1, delay=0, is_host=True)
            for link in links:
                # delete ports linked switches
                self.port_info[link.src.dpid].discard(link.src.port_no)
                self.port_info[link.dst.dpid].discard(link.dst.port_no)

                # s1 -> s2: s1.port, s2 -> s1: s2.port
                self.port_link[(link.src.dpid,link.src.port_no)]=(link.src.dpid, link.dst.dpid)
                self.port_link[(link.dst.dpid,link.dst.port_no)] = (link.dst.dpid, link.src.dpid)

                self.link_info[(link.src.dpid, link.dst.dpid)] = link.src.port_no
                self.link_info[(link.dst.dpid, link.src.dpid)] = link.dst.port_no
                

                self.topo_map.add_edge(link.src.dpid, link.dst.dpid, hop=1, is_host=False)

    def shortest_path(self, src, dst, weight='hop'):
        try:
            paths = list(nx.shortest_simple_paths(self.topo_map, src, dst, weight=weight))
            return paths[0]
        except:
            self.logger.info('host not find/no path')

    def show_topo_map(self):
        self.logger.info('topo map:')
        self.logger.info('{:^10s}  ->  {:^10s}      {:^10s}'.format('node', 'node', 'delay'))
        for src, dst in self.topo_map.edges:
            self.logger.info('{:^10s}      {:^10s}      {:^10s}'.format(str(src), str(dst), f"{self.topo_map.edges[src,dst].get('delay', -0.001)*1000:.0f}ms"))
        self.logger.info('\n')

