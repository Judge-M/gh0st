import React, { useState, useEffect, useRef } from 'react';
import * as Crypto from 'expo-crypto';
import * as SecureStore from 'expo-secure-store';
import * as Notifications from 'expo-notifications';
import * as Device from 'expo-device';
import Constants from 'expo-constants';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { Envelope, PairCipher, Pairing, parsePairingUri } from './relayCrypto';
import {
  StyleSheet,
  Text,
  View,
  ScrollView,
  TouchableOpacity,
  TextInput,
  SafeAreaView,
  StatusBar,
  Alert,
  Platform,
  AppState
} from 'react-native';

const STORED_PAIRING_KEY = 'gh0st.pairing.v1';
const STORED_PUSH_TOKEN_KEY = 'gh0st.push-token.v1';

Notifications.setNotificationHandler({
  handleNotification: async () => ({ shouldShowAlert: true, shouldPlaySound: false, shouldSetBadge: false }),
});

export default function Gh0stMobileApp() {
  const [pairingUri, setPairingUri] = useState('');
  const [taskId, setTaskId] = useState('');
  const [isConnected, setIsConnected] = useState(false);
  const [isPaired, setIsPaired] = useState(false);
  const [pairingStatus, setPairingStatus] = useState('Disconnected');
  const [hasSavedPairing, setHasSavedPairing] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [prompt, setPrompt] = useState('');
  const [events, setEvents] = useState<any[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState<any[]>([]);
  const [quotaStats, setQuotaStats] = useState<{remainingUsd: number | null; remainingTokens: number | null}>({ remainingUsd: null, remainingTokens: null });

  const ws = useRef<WebSocket | null>(null);
  const cipher = useRef<PairCipher | null>(null);
  const activePairing = useRef<Pairing | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttempts = useRef(0);
  const activePushToken = useRef<string | null>(null);
  const scanned = useRef(false);
  const [cameraPermission, requestCameraPermission] = useCameraPermissions();

  const connectWithPairing = (pairing: Pairing) => {
    try {
      activePairing.current = pairing;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      const channel = new PairCipher(pairing, 'client');
      ws.current?.close();
      cipher.current = channel;
      setIsPaired(false);
      setPairingStatus('Connecting...');
      const socket = new WebSocket(pairing.relay);

      socket.onopen = () => {
        if (ws.current !== socket) return;
        setIsConnected(true);
        setPairingStatus('Connected; verifying pairing');
        socket.send(JSON.stringify({ message_type: 'client_register', pair_id: pairing.pair }));
      };

      socket.onmessage = (event) => {
        if (ws.current !== socket) return;
        try {
          const data = JSON.parse(event.data);
          if (data.message_type === 'transport_connected') return;
          if (data.message_type !== 'ciphertext') return;
          const decrypted = channel.decrypt(data as Envelope);
          if (decrypted.message_type === 'paired') {
            reconnectAttempts.current = 0;
            setIsPaired(true);
            setPairingStatus(`Paired with ${pairing.device}`);
            if (Platform.OS !== 'web') {
              void SecureStore.setItemAsync(STORED_PAIRING_KEY, JSON.stringify(pairing))
                .then(() => setHasSavedPairing(true))
                .catch(() => setPairingStatus(`Paired with ${pairing.device}; secure reconnect storage failed`));
            }
          } else {
            handleIncomingEvent(decrypted);
          }
        } catch (e) {
          setPairingStatus('Invalid encrypted message');
        }
      };

      socket.onerror = () => {
        if (ws.current !== socket) return;
        setPairingStatus('Connection Error');
        setIsConnected(false);
      };

      socket.onclose = (event) => {
        if (ws.current !== socket) return;
        setIsConnected(false);
        setIsPaired(false);
        cipher.current = null;
        if (event.code === 1008 && event.reason === 'Pairing revoked') {
          activePairing.current = null;
          if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
          setHasSavedPairing(false);
          setPairingStatus('This pairing was revoked by the operator');
          if (Platform.OS !== 'web') void SecureStore.deleteItemAsync(STORED_PAIRING_KEY);
          return;
        }
        if (activePairing.current?.pair === pairing.pair) {
          setPairingStatus('Disconnected; reconnecting securely…');
          reconnectAttempts.current += 1;
          const delay = Math.min(30000, 1000 * (2 ** Math.min(reconnectAttempts.current - 1, 5)));
          reconnectTimer.current = setTimeout(() => connectWithPairing(pairing), delay);
        } else {
          setPairingStatus('Disconnected');
        }
      };

      ws.current = socket;
    } catch (err) {
      setPairingStatus(err instanceof Error ? err.message : 'Invalid pairing URI');
    }
  };

  const handleIncomingEvent = (data: any) => {
    setEvents((prev) => [data, ...prev.slice(0, 49)]);
    if (data.message_type === 'action_result' && !data.ok) {
      Alert.alert('Request failed', data.error || 'Unknown error');
    }
    const snapshot = data.message_type === 'action_result' ? data.result : data;
    if (snapshot && typeof snapshot === 'object') {
      if (snapshot.task_id) setTaskId(snapshot.task_id);
      if (Array.isArray(snapshot.grant_requests)) {
        setPendingApprovals(snapshot.grant_requests.filter((grant: any) => grant.status === 'pending'));
      }
      setQuotaStats((prev) => ({
        remainingUsd: snapshot.remaining_usd ?? prev.remainingUsd,
        remainingTokens: snapshot.remaining_tokens ?? prev.remainingTokens,
      }));
    }
  };

  const connectWithUri = (uri: string) => {
    try {
      setPairingUri('');
      connectWithPairing(parsePairingUri(uri));
    } catch (err) {
      setPairingStatus(err instanceof Error ? err.message : 'Invalid pairing URI');
    }
  };

  const forgetPairing = async () => {
    if (activePushToken.current) void sendRemoteAction('unregister_push');
    activePushToken.current = null;
    activePairing.current = null;
    if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    ws.current?.close();
    ws.current = null;
    cipher.current = null;
    setIsConnected(false);
    setIsPaired(false);
    setHasSavedPairing(false);
    setPairingStatus('Saved pairing removed from this phone');
    if (Platform.OS !== 'web') {
      await Promise.all([
        SecureStore.deleteItemAsync(STORED_PAIRING_KEY),
        SecureStore.deleteItemAsync(STORED_PUSH_TOKEN_KEY),
      ]);
    }
  };

  useEffect(() => {
    let active = true;
    const appState = AppState.addEventListener('change', (state) => {
      const pairing = activePairing.current;
      if (state === 'active' && pairing &&
          (!ws.current || ws.current.readyState === WebSocket.CLOSED)) {
        connectWithPairing(pairing);
      }
    });
    if (Platform.OS !== 'web') {
      void SecureStore.getItemAsync(STORED_PAIRING_KEY).then((value) => {
        if (!active || !value) return;
        const saved = JSON.parse(value);
        const pairing = parsePairingUri(`gh0st://pair?relay=${encodeURIComponent(saved.relay)}&device=${encodeURIComponent(saved.device)}&pair=${encodeURIComponent(saved.pair)}&secret=${encodeURIComponent(saved.secret)}`);
        setHasSavedPairing(true);
        setPairingStatus('Reconnecting to saved device…');
        connectWithPairing(pairing);
      }).catch(() => {
        if (active) setPairingStatus('Saved pairing could not be read; scan a new code');
      });
    }
    return () => {
      active = false;
      activePairing.current = null;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      appState.remove();
      ws.current?.close();
    };
  }, []);

  const connectToRelay = () => connectWithUri(pairingUri);

  const handleQrScan = ({ data }: { data: string }) => {
    if (scanned.current) return;
    try {
      parsePairingUri(data);
      scanned.current = true;
      setPairingUri(data);
      setScanning(false);
      connectWithUri(data);
    } catch {
      setPairingStatus('That QR code is not a gh0st pairing code');
      setScanning(false);
    }
  };

  const sendRemoteAction = async (action: string, payload: Record<string, unknown> = {}) => {
    const socket = ws.current;
    const channel = cipher.current;
    if (!isPaired || !socket || !channel || socket.readyState !== WebSocket.OPEN) {
      Alert.alert('Error', 'Pair this device with a gh0st instance first');
      return;
    }
    try {
      const random = await Crypto.getRandomBytesAsync(8);
      const actionId = `act_${Array.from(random).map(value => value.toString(16).padStart(2, '0')).join('')}`;
      const command = { action_id: actionId, action_type: action, ...payload };
      const envelope = await channel.encrypt(command, Crypto.getRandomBytesAsync);
      if (socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify(envelope));
    } catch (error) {
      Alert.alert('Request failed', error instanceof Error ? error.message : 'Could not encrypt request');
    }
  };

  const registerPushToken = async () => {
    if (Platform.OS === 'web' || !Device.isDevice) {
      setPairingStatus('Paired; push alerts need a physical device');
      return;
    }
    try {
      let permission = await Notifications.getPermissionsAsync();
      if (!permission.granted && permission.status !== 'granted') {
        permission = await Notifications.requestPermissionsAsync();
      }
      if (!permission.granted && permission.status !== 'granted') {
        setPairingStatus('Paired; push alerts are disabled');
        return;
      }
      const projectId = Constants.expoConfig?.extra?.eas?.projectId;
      if (!projectId) {
        setPairingStatus('Paired; link this app to an Expo project to enable push alerts');
        return;
      }
      await Notifications.setNotificationChannelAsync('default', {
        name: 'gh0st updates', importance: Notifications.AndroidImportance.DEFAULT,
      });
      const token = (await Notifications.getExpoPushTokenAsync({ projectId })).data;
      activePushToken.current = token;
      await SecureStore.setItemAsync(STORED_PUSH_TOKEN_KEY, token);
      await sendRemoteAction('register_push', { token });
      setPairingStatus(`Paired with ${activePairing.current?.device}; push alerts enabled`);
    } catch {
      setPairingStatus('Paired; push alerts could not be enabled');
    }
  };

  const restorePushToken = async () => {
    if (Platform.OS === 'web') return;
    try {
      const token = await SecureStore.getItemAsync(STORED_PUSH_TOKEN_KEY);
      if (!token) return;
      activePushToken.current = token;
      await sendRemoteAction('register_push', { token });
    } catch {
      setPairingStatus('Paired; saved push alert registration could not be restored');
    }
  };

  useEffect(() => {
    if (isPaired) void restorePushToken();
  }, [isPaired]);

  const dispatchNewTask = () => {
    if (!prompt.trim()) return;
    void sendRemoteAction('dispatch_task', { prompt: prompt.trim() });
    setPrompt('');
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

        <View style={styles.card}>
          <Text style={styles.cardTitle}>Pair this device</Text>
          {!scanning ? (
            <TouchableOpacity style={styles.secondaryBtn} onPress={() => {
              scanned.current = false;
              setScanning(true);
            }}>
              <Text style={styles.primaryBtnText}>Scan pairing QR code</Text>
            </TouchableOpacity>
          ) : !cameraPermission ? (
            <Text style={styles.emptyText}>Checking camera permission…</Text>
          ) : !cameraPermission.granted ? (
            <TouchableOpacity style={styles.primaryBtn} onPress={() => void requestCameraPermission()}>
              <Text style={styles.primaryBtnText}>Allow camera access to scan</Text>
            </TouchableOpacity>
          ) : (
            <View style={styles.cameraFrame}>
              <CameraView
                style={styles.camera}
                facing="back"
                barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
                onBarcodeScanned={handleQrScan}
              />
              <TouchableOpacity style={styles.cancelScanBtn} onPress={() => setScanning(false)}>
                <Text style={styles.primaryBtnText}>Cancel scan</Text>
              </TouchableOpacity>
            </View>
          )}
          <TextInput
            style={styles.input}
            placeholder="Paste the one-time gh0st://pair URI"
            placeholderTextColor="#64748b"
            value={pairingUri}
            onChangeText={setPairingUri}
            autoCapitalize="none"
            autoCorrect={false}
            multiline
          />
          <TouchableOpacity style={styles.primaryBtn} onPress={connectToRelay}>
            <Text style={styles.primaryBtnText}>Connect securely</Text>
          </TouchableOpacity>
          {hasSavedPairing && (
            <TouchableOpacity style={styles.secondaryBtn} onPress={() => void forgetPairing()}>
              <Text style={styles.primaryBtnText}>Forget saved pairing on this phone</Text>
            </TouchableOpacity>
          )}
          {isPaired && Platform.OS !== 'web' && (
            <TouchableOpacity style={styles.secondaryBtn} onPress={() => void registerPushToken()}>
              <Text style={styles.primaryBtnText}>Enable push alerts</Text>
            </TouchableOpacity>
          )}
        </View>

        <View style={styles.card}>
          <Text style={styles.cardTitle}>Task status</Text>
          <TextInput
            style={styles.input}
            placeholder="Task ID"
            placeholderTextColor="#64748b"
            value={taskId}
            onChangeText={setTaskId}
            autoCapitalize="none"
          />
          <TouchableOpacity style={styles.primaryBtn}
            onPress={() => void sendRemoteAction('status', { task_id: taskId.trim() })}
            disabled={!isPaired || !taskId.trim()}>
            <Text style={styles.primaryBtnText}>Refresh status and grants</Text>
          </TouchableOpacity>
        </View>

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
                <Text style={styles.codeText}>{approval.action_type}: {JSON.stringify(approval.details)}</Text>
                <View style={styles.buttonRow}>
                  <TouchableOpacity
                    style={[styles.btn, styles.btnSuccess]}
                    onPress={() => void sendRemoteAction('approve_grant',
                      { task_id: approval.task_id, grant_id: approval.grant_id })}
                  >
                    <Text style={styles.btnText}>Approve</Text>
                  </TouchableOpacity>
                  <TouchableOpacity
                    style={[styles.btn, styles.btnDanger]}
                    onPress={() => void sendRemoteAction('reject_grant',
                      { task_id: approval.task_id, grant_id: approval.grant_id })}
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
                  <Text style={styles.eventType}>{evt.message_type || evt.event_type || 'system_event'}</Text>
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
  secondaryBtn: {
    backgroundColor: '#334155',
    paddingVertical: 12,
    borderRadius: 8,
    alignItems: 'center',
    marginBottom: 12,
  },
  cameraFrame: {
    height: 260,
    overflow: 'hidden',
    borderRadius: 8,
    marginBottom: 12,
  },
  camera: {
    flex: 1,
  },
  cancelScanBtn: {
    position: 'absolute',
    bottom: 10,
    alignSelf: 'center',
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
    backgroundColor: '#334155',
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
