import React, { useState, useEffect, useRef } from 'react';
import {
  StyleSheet,
  Text,
  View,
  ScrollView,
  TouchableOpacity,
  TextInput,
  SafeAreaView,
  StatusBar,
  ActivityIndicator,
  Alert,
  Platform
} from 'react-native';

/**
 * gh0st Mobile Remote Control Client (iOS & Android)
 * Connects to a configured relay endpoint when a broker is available.
 */

const RELAY_ENDPOINT = process.env.EXPO_PUBLIC_GH0ST_RELAY_URL || '';

export default function Gh0stMobileApp() {
  const [deviceId, setDeviceId] = useState('dev_mobile_client');
  const [isConnected, setIsConnected] = useState(false);
  const [isPaired, setIsPaired] = useState(false);
  const [pairingStatus, setPairingStatus] = useState('Disconnected');
  const [prompt, setPrompt] = useState('');
  const [events, setEvents] = useState<any[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState<any[]>([]);
  const [quotaStats, setQuotaStats] = useState<{remainingUsd: number | null; remainingTokens: number | null}>({ remainingUsd: null, remainingTokens: null });

  const ws = useRef<WebSocket | null>(null);

  useEffect(() => {
    connectToRelay();
    return () => {
      ws.current?.close();
    };
  }, []);

  const connectToRelay = () => {
    if (!RELAY_ENDPOINT.startsWith('wss://')) {
      setPairingStatus('Set EXPO_PUBLIC_GH0ST_RELAY_URL');
      return;
    }
    setPairingStatus('Connecting...');
    try {
      const socket = new WebSocket(RELAY_ENDPOINT);

      socket.onopen = () => {
        setIsConnected(true);
        setPairingStatus('Connected; waiting for pairing');
        // Send registration handshake
        socket.send(JSON.stringify({
          message_type: 'client_register',
          device_id: deviceId,
          timestamp: new Date().toISOString()
        }));
      };

      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          handleIncomingEvent(data);
        } catch (e) {
          console.error('Failed to parse WebSocket message:', e);
        }
      };

      socket.onerror = (error) => {
        setPairingStatus('Connection Error');
        setIsConnected(false);
      };

      socket.onclose = () => {
        setIsConnected(false);
        setIsPaired(false);
        setPairingStatus('Disconnected (Reconnecting in 5s...)');
        setTimeout(connectToRelay, 5000);
      };

      ws.current = socket;
    } catch (err) {
      setPairingStatus('Failed to connect');
    }
  };

  const handleIncomingEvent = (data: any) => {
    if (data.message_type === 'paired') {
      setIsPaired(true);
      setPairingStatus('Paired');
    }
    setEvents((prev) => [data, ...prev.slice(0, 49)]);

    if (data.event_type === 'approval_requested') {
      setPendingApprovals((prev) => [...prev, data.payload]);
    } else if (data.event_type === 'quota_warning') {
      setQuotaStats((prev) => ({
        ...prev,
        remainingUsd: data.payload.remaining_usd ?? prev.remainingUsd,
        remainingTokens: data.payload.remaining_tokens ?? prev.remainingTokens
      }));
    }
  };

  const sendRemoteAction = (action: string, ticketId?: string, payload: any = {}) => {
    if (!isPaired || !ws.current || ws.current.readyState !== WebSocket.OPEN) {
      Alert.alert('Error', 'Pair this device with a gh0st relay first');
      return;
    }

    const command = {
      message_type: 'action_command',
      action_id: `act_${Date.now()}`,
      action_type: action,
      task_id: payload.task_id || 'new',
      ticket_id: ticketId,
      parameters: payload,
      timestamp: new Date().toISOString()
    };

    ws.current.send(JSON.stringify(command));

    if (action === 'approve_grant' || action === 'reject_grant') {
      setPendingApprovals((prev) => prev.filter((item) => item.ticket_id !== ticketId));
    }
  };

  const dispatchNewTask = () => {
    if (!prompt.trim()) return;

    sendRemoteAction('dispatch_task', undefined, { prompt: prompt.trim() });
    setPrompt('');
    Alert.alert('Request sent', 'Waiting for confirmation from the paired instance.');
  };

  return (
    <SafeAreaView style={styles.container}>
      <StatusBar barStyle="light-content" backgroundColor="#0f172a" />

      {/* Header Bar */}
      <View style={styles.header}>
        <View style={styles.titleRow}>
          <Text style={styles.logoText}>👻 gh0st Mobile</Text>
          <View style={[styles.badge, isConnected ? styles.badgeSuccess : styles.badgeDanger]}>
            <Text style={styles.badgeText}>{isConnected ? 'CONNECTED' : 'OFFLINE'}</Text>
          </View>
        </View>
        <Text style={styles.subtext}>
          Platform: {Platform.OS.toUpperCase()} | Tunnel: {pairingStatus}
        </Text>
      </View>

      {/* Main Controls & Event Feed */}
      <ScrollView style={styles.scrollContent} contentContainerStyle={{ paddingBottom: 30 }}>

        {/* Quota & Health Widget */}
        <View style={styles.card}>
          <Text style={styles.cardTitle}>📊 Instance Budget & Quota</Text>
          <View style={styles.statsGrid}>
            <View style={styles.statBox}>
              <Text style={styles.statLabel}>Remaining USD</Text>
              <Text style={styles.statValue}>{quotaStats.remainingUsd === null ? '—' : `$${quotaStats.remainingUsd.toFixed(2)}`}</Text>
            </View>
            <View style={styles.statBox}>
              <Text style={styles.statLabel}>Remaining Tokens</Text>
              <Text style={styles.statValue}>{quotaStats.remainingTokens === null ? '—' : quotaStats.remainingTokens.toLocaleString()}</Text>
            </View>
          </View>
        </View>

        {/* Human-in-the-Loop Approvals */}
        {pendingApprovals.length > 0 && (
          <View style={[styles.card, styles.alertCard]}>
            <Text style={styles.alertTitle}>⚠️ Action Required: Approval Requested</Text>
            {pendingApprovals.map((approval, idx) => (
              <View key={idx} style={styles.approvalBox}>
                <Text style={styles.approvalText}>
                  Ticket <Text style={styles.bold}>{approval.ticket_id}</Text> requires tool grant:
                </Text>
                <Text style={styles.codeText}>{approval.tool_name || 'Budget Expansion'}</Text>
                <View style={styles.buttonRow}>
                  <TouchableOpacity
                    style={[styles.btn, styles.btnSuccess]}
                    onPress={() => sendRemoteAction('approve_grant', approval.ticket_id)}
                  >
                    <Text style={styles.btnText}>Approve</Text>
                  </TouchableOpacity>
                  <TouchableOpacity
                    style={[styles.btn, styles.btnDanger]}
                    onPress={() => sendRemoteAction('reject_grant', approval.ticket_id)}
                  >
                    <Text style={styles.btnText}>Reject</Text>
                  </TouchableOpacity>
                </View>
              </View>
            ))}
          </View>
        )}

        {/* Remote Prompt Dispatch */}
        <View style={styles.card}>
          <Text style={styles.cardTitle}>🚀 Dispatch Remote Task</Text>
          <TextInput
            style={styles.input}
            placeholder="Type task prompt (e.g. 'Run pytest on auth module')..."
            placeholderTextColor="#64748b"
            value={prompt}
            onChangeText={setPrompt}
            multiline
          />
          <TouchableOpacity style={styles.primaryBtn} onPress={dispatchNewTask} disabled={!isPaired}>
            <Text style={styles.primaryBtnText}>Dispatch Task via Tunnel</Text>
          </TouchableOpacity>
        </View>

        {/* Real-time Reverse Tunnel Event Stream */}
        <View style={styles.card}>
          <Text style={styles.cardTitle}>📡 Live Reverse Tunnel Stream</Text>
          {events.length === 0 ? (
            <Text style={styles.emptyText}>Waiting for real-time events from local daemon...</Text>
          ) : (
            events.map((evt, idx) => (
              <View key={idx} style={styles.eventItem}>
                <View style={styles.eventHeader}>
                  <Text style={styles.eventType}>{evt.event_type || 'system_event'}</Text>
                  <Text style={styles.eventTime}>
                    {evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : ''}
                  </Text>
                </View>
                <Text style={styles.eventBody}>
                  {JSON.stringify(evt.payload || evt, null, 2)}
                </Text>
              </View>
            ))
          )}
        </View>

      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#090d16',
  },
  header: {
    padding: 16,
    backgroundColor: '#0f172a',
    borderBottomWidth: 1,
    borderBottomColor: '#1e293b',
  },
  titleRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  logoText: {
    fontSize: 22,
    fontWeight: 'bold',
    color: '#f8fafc',
  },
  subtext: {
    fontSize: 12,
    color: '#94a3b8',
    marginTop: 4,
  },
  badge: {
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 6,
  },
  badgeSuccess: {
    backgroundColor: '#059669',
  },
  badgeDanger: {
    backgroundColor: '#dc2626',
  },
  badgeText: {
    color: '#ffffff',
    fontSize: 10,
    fontWeight: 'bold',
  },
  scrollContent: {
    padding: 16,
  },
  card: {
    backgroundColor: '#1e293b',
    borderRadius: 12,
    padding: 16,
    marginBottom: 16,
    borderWidth: 1,
    borderColor: '#334155',
  },
  alertCard: {
    borderColor: '#f59e0b',
    backgroundColor: '#1e1b18',
  },
  cardTitle: {
    fontSize: 16,
    fontWeight: '600',
    color: '#f1f5f9',
    marginBottom: 12,
  },
  alertTitle: {
    fontSize: 16,
    fontWeight: 'bold',
    color: '#f59e0b',
    marginBottom: 12,
  },
  statsGrid: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  statBox: {
    flex: 1,
    backgroundColor: '#0f172a',
    padding: 12,
    borderRadius: 8,
    marginRight: 8,
  },
  statLabel: {
    fontSize: 12,
    color: '#94a3b8',
  },
  statValue: {
    fontSize: 18,
    fontWeight: 'bold',
    color: '#38bdf8',
    marginTop: 4,
  },
  input: {
    backgroundColor: '#0f172a',
    color: '#f8fafc',
    borderRadius: 8,
    padding: 12,
    fontSize: 14,
    minHeight: 80,
    textAlignVertical: 'top',
    borderColor: '#334155',
    borderWidth: 1,
    marginBottom: 12,
  },
  primaryBtn: {
    backgroundColor: '#2563eb',
    paddingVertical: 12,
    borderRadius: 8,
    alignItems: 'center',
  },
  primaryBtnText: {
    color: '#ffffff',
    fontWeight: 'bold',
    fontSize: 14,
  },
  approvalBox: {
    backgroundColor: '#0f172a',
    padding: 12,
    borderRadius: 8,
    marginBottom: 8,
  },
  approvalText: {
    color: '#e2e8f0',
    fontSize: 13,
  },
  bold: {
    fontWeight: 'bold',
    color: '#38bdf8',
  },
  codeText: {
    fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace',
    color: '#f43f5e',
    marginVertical: 4,
  },
  buttonRow: {
    flexDirection: 'row',
    marginTop: 8,
  },
  btn: {
    flex: 1,
    paddingVertical: 8,
    borderRadius: 6,
    alignItems: 'center',
    marginRight: 8,
  },
  btnSuccess: {
    backgroundColor: '#16a34a',
  },
  btnDanger: {
    backgroundColor: '#e11d48',
  },
  btnText: {
    color: '#ffffff',
    fontWeight: 'bold',
    fontSize: 12,
  },
  emptyText: {
    color: '#64748b',
    fontStyle: 'italic',
    fontSize: 13,
  },
  eventItem: {
    backgroundColor: '#0f172a',
    padding: 10,
    borderRadius: 6,
    marginBottom: 8,
  },
  eventHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    marginBottom: 4,
  },
  eventType: {
    color: '#38bdf8',
    fontSize: 12,
    fontWeight: 'bold',
  },
  eventTime: {
    color: '#64748b',
    fontSize: 10,
  },
  eventBody: {
    fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace',
    color: '#cbd5e1',
    fontSize: 11,
  },
});
