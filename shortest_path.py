# os_ken-manager shortest_forward.py --observe-links
from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, HANDSHAKE_DISPATCHER
from os_ken.controller.handler import set_ev_cls
from os_ken.controller.handler import set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet
from os_ken.lib.packet import ethernet, arp, ipv4, lldp
from os_ken.controller import ofp_event
from os_ken.topology import event
from os_ken.topology.switches import LLDPPacket
import sys
from network_awareness import NetworkAwareness
import networkx as nx
from os_ken.base.app_manager import lookup_service_brick

ETHERNET = ethernet.ethernet.__name__
ETHERNET_MULTICAST = "ff:ff:ff:ff:ff:ff"
ARP = arp.arp.__name__
class ShortestPath(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {
        'network_awareness': NetworkAwareness
    }

    def __init__(self, *args, **kwargs):
        super(ShortestPath, self).__init__(*args, **kwargs)
        self.network_awareness = kwargs['network_awareness']
        # 权重改为delay
        self.weight = 'delay'
        self.mac_to_port = {}
        self.sw = {}
        # 存下当前有效的所有路径，当链路变化的时候通知交换机
        self.path = []
        self.switches = None
        self.network_awareness.weight = self.weight

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        dp = datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=dp, priority=priority,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
            match=match, instructions=inst)
        dp.send_msg(mod)

    def delete_flow(self, datapath, port):
        '''
        对datapath删除包含指定port的所有流表项
        '''
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        # 入端口为port的流表项
        mod = parser.OFPFlowMod(
            command=ofp.OFPFC_DELETE,
            datapath=datapath, match=parser.OFPMatch(in_port=port),
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
        )
        datapath.send_msg(mod)
        
        # 出端口为port的流表项
        mod = parser.OFPFlowMod(
            command=ofp.OFPFC_DELETE,
            datapath=datapath, match=parser.OFPMatch(),
            out_port=port, out_group=ofp.OFPG_ANY,
        )
        datapath.send_msg(mod)

    def delete_flow_priority(self, datapath):
        '''
        对datapath删除包含指定port的所有流表项
        '''
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        # 入端口为port的流表项
        mod = parser.OFPFlowMod(
            command=ofp.OFPFC_DELETE,
            datapath=datapath, match=parser.OFPMatch(),
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
            priority=1,
        )
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        arp_pkt = pkt.get_protocol(arp.arp)
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)

        pkt_type = eth_pkt.ethertype

        # layer 2 self-learning
        dst_mac = eth_pkt.dst
        src_mac = eth_pkt.src

        if isinstance(arp_pkt, arp.arp):
            self.handle_arp(msg, in_port, dst_mac,src_mac, pkt,pkt_type)

        if isinstance(ipv4_pkt, ipv4.ipv4):
            self.handle_ipv4(msg, ipv4_pkt.src, ipv4_pkt.dst, pkt_type)

    def handle_arp(self, msg, in_port, dst,src, pkt,pkt_type):
        # just handle loop here
        # just like your code in exp1 mission2
        dp = msg.datapath
        parser = dp.ofproto_parser
        dpid = dp.id
        ofp = dp.ofproto
        arp_pkt = pkt.get_protocol(arp.arp) # 获取ARP数据包
        key = (dp.id, arp_pkt.src_mac, arp_pkt.dst_ip)   # 构造字典键
        if not key in self.sw:              # 如果不在字典里，增加一条映射
            self.sw[key] = in_port
        else:                               # 下次收到时，若in_port不同，直接丢弃。
            if in_port != self.sw[key]:
                return
        if not dpid in self.mac_to_port:
            self.mac_to_port[dpid] = {}
        # 学习映射
        self.mac_to_port[dpid][src] = in_port
        if not dst in self.mac_to_port[dpid]:
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)] # 如果未学习，则洪泛数据包
        else:
            actions = [parser.OFPActionOutput(self.mac_to_port[dpid][dst])]   # 如果已学习，则向指定端⼝转发数据包 
            match = parser.OFPMatch(eth_dst=dst)  
            # 设置流表及其超时时间，使之能够适应拓扑变化
            self.add_flow(dp, 1, match, actions, 10, 30)
        out = parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id, in_port=msg.match['in_port'],actions=actions, data=msg.data)
        dp.send_msg(out)

    def handle_ipv4(self, msg, src_ip, dst_ip, pkt_type):
        parser = msg.datapath.ofproto_parser

        dpid_path = self.network_awareness.shortest_path(src_ip, dst_ip,weight=self.weight)
        if not dpid_path:
            return



        self.path.append(dpid_path)
        # get port path:  h1 -> in_port, s1, out_port -> h2
        port_path = []
        for i in range(1, len(dpid_path) - 1):
            in_port = self.network_awareness.link_info[(dpid_path[i], dpid_path[i - 1])]
            out_port = self.network_awareness.link_info[(dpid_path[i], dpid_path[i + 1])]
            port_path.append((in_port, dpid_path[i], out_port))
        self.show_path(src_ip, dst_ip, port_path)
        # calc path delay
        delay = 0
        for i in range(1, len(dpid_path) - 2):
            src = dpid_path[i]
            dst = dpid_path[i+1]
            delay += self.network_awareness.topo_map[src][dst]['delay']
        print(f"delay: {delay*1000:.0f}ms")
        print(f"RTT: {delay*2000:.0f}ms")
        # send flow mod
        for node in port_path:
            in_port, dpid, out_port = node
            self.send_flow_mod(parser, dpid, pkt_type, src_ip, dst_ip, in_port, out_port)
            self.send_flow_mod(parser, dpid, pkt_type, dst_ip, src_ip, out_port, in_port)

        # send packet_out
        _, dpid, out_port = port_path[-1]
        dp = self.network_awareness.switch_info[dpid]
        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id, in_port=in_port, actions=actions, data=msg.data)
        dp.send_msg(out)
    
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        '''
        处理链路端口变化
        '''
        msg = ev.msg
        dp = msg.datapath
        port = msg.desc.port_no
        reason = msg.reason
        ofproto = dp.ofproto
        # 若端口消失
        if reason == ofproto.OFPPR_DELETE or (reason == ofproto.OFPPR_MODIFY and msg.desc.state in [ofproto.OFPPS_LINK_DOWN, ofproto.OFPPS_BLOCKED]):
            # 获取错误的路径
            error_path = []
            for dpid_path in self.path:
                for i in range(1, len(dpid_path) - 1):
                    in_port = self.network_awareness.link_info[(dpid_path[i], dpid_path[i - 1])]
                    out_port = self.network_awareness.link_info[(dpid_path[i], dpid_path[i + 1])]
                    if dpid_path[i] == dp.id and port in [in_port, out_port]:
                        error_path.append(dpid_path)
            # 删除这个路径，并通知交换机删除流表项
            for dpid_path in error_path:
                self.path.remove(dpid_path)
                for i in range(1, len(dpid_path) - 1):
                    in_port = self.network_awareness.link_info[(dpid_path[i], dpid_path[i - 1])]
                    out_port = self.network_awareness.link_info[(dpid_path[i], dpid_path[i + 1])]
                    self.delete_flow(self.network_awareness.switch_info[dpid_path[i]], in_port)
                    self.delete_flow(self.network_awareness.switch_info[dpid_path[i]], out_port)

            # 删除ARP相关项
            to_discard = []
            for key in self.sw:
                dpid, _, _ = key
                if dpid == dp.id and self.sw[key] == port: 
                    to_discard.append(key)
            for key in to_discard:
                self.sw.pop(key, None)
            
            to_discard = []
            self.mac_to_port = {}
            self.sw = {}
        
        # 若端口新增
        else:
            # 通知交换机删除所有流表项，让其重新发消息到控制器获取新的最短路径。
            for dpid_path in self.path:
                self.path.remove(dpid_path)
                for i in range(1, len(dpid_path) - 1):
                    in_port = self.network_awareness.link_info[(dpid_path[i], dpid_path[i - 1])]
                    out_port = self.network_awareness.link_info[(dpid_path[i], dpid_path[i + 1])]
                    self.delete_flow(self.network_awareness.switch_info[dpid_path[i]], in_port)
                    self.delete_flow(self.network_awareness.switch_info[dpid_path[i]], out_port)
        # 调用network_awareness的方法让其应对变化
        self.network_awareness.port_status_handler(ev)


    def send_flow_mod(self, parser, dpid, pkt_type, src_ip, dst_ip, in_port, out_port):
        dp = self.network_awareness.switch_info[dpid]
        match = parser.OFPMatch(
            in_port=in_port, eth_type=pkt_type, ipv4_src=src_ip, ipv4_dst=dst_ip)
        actions = [parser.OFPActionOutput(out_port)]
        self.add_flow(dp, 2, match, actions, 10, 30)

    def show_path(self, src, dst, port_path):
        self.logger.info('path: {} -> {}'.format(src, dst))
        path = src + ' -> '
        for node in port_path:
            path += '{}:s{}:{}'.format(*node) + ' -> '
        path += dst
        self.logger.info(path)