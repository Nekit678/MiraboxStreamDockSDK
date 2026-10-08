(() => {
  "use strict";

  /**
   * Determine whether a value is a non-null object rather than an array.
   *
   * @param {*} value Value to inspect.
   * @returns {value is Object<string, *>} Whether the value is an object.
   */
  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  /**
   * Parse a JSON string or validate an already-decoded object.
   *
   * @param {string|Object<string, *>} value Encoded or decoded object.
   * @param {string} name Human-readable input name used in validation errors.
   * @returns {Object<string, *>} The decoded object.
   * @throws {TypeError} If the string is invalid JSON or the result is not an object.
   */
  function parseObject(value, name) {
    let parsed = value;
    if (typeof value === "string") {
      try {
        parsed = JSON.parse(value);
      } catch {
        throw new TypeError(`${name} must contain valid JSON`);
      }
    }
    if (!isObject(parsed)) {
      throw new TypeError(`${name} must be an object`);
    }
    return parsed;
  }

  /** Return an owned snapshot using the same JSON rules as wire messages. */
  function snapshotObject(value, name) {
    return parseObject(JSON.stringify(value), name);
  }

  /**
   * Browser-side client shared by a Stream Dock Property Inspector.
   *
   * Stream Dock initializes the singleton through
   * {@link window.connectElgatoStreamDeckSocket}. Consumers normally use the
   * {@link window.MiraBoxPropertyInspector} instance instead of constructing a
   * client. Start application work in `connected`. Messages sent while the
   * WebSocket is connecting are queued as JSON and flushed after registration.
   */
  class MiraBoxPropertyInspectorClient {
    /** Initialize disconnected client state and an empty settings snapshot. */
    constructor() {
      this._listeners = new Map();
      this._pendingMessages = [];
      this._websocket = undefined;
      this._action = undefined;
      this._context = undefined;
      this._settings = {};
      this._info = {};
      this._actionInfo = {};
    }

    /**
     * Manifest UUID of the action edited by this Property Inspector.
     *
     * @returns {string|undefined} Action UUID, or `undefined` before connection setup.
     */
    get action() {
      return this._action;
    }

    /**
     * Opaque action-context identifier supplied by Stream Dock.
     *
     * @returns {string|undefined} Context ID, or `undefined` before connection setup.
     */
    get context() {
      return this._context;
    }

    /**
     * Return a deeply isolated JSON snapshot of the latest action settings.
     *
     * The snapshot is initialized from `actionInfo` and refreshed on every
     * `didReceiveSettings` message or accepted local settings update. Accepted
     * updates are optimistic; they do not acknowledge persistence by the host.
     *
     * @returns {Object<string, *>} Current settings snapshot.
     */
    get settings() {
      return snapshotObject(this._settings, "settings");
    }

    /**
     * Registration metadata supplied to the Property Inspector callback.
     *
     * @returns {Object<string, *>} Parsed Stream Dock information object.
     */
    get info() {
      return this._info;
    }

    /**
     * Metadata describing the action instance and its initial payload.
     *
     * @returns {Object<string, *>} Parsed action information object.
     */
    get actionInfo() {
      return this._actionInfo;
    }

    /**
     * Whether the Property Inspector WebSocket is currently open.
     *
     * @returns {boolean} `true` only while the socket is in `WebSocket.OPEN` state.
     */
    get isConnected() {
      return this._websocket?.readyState === WebSocket.OPEN;
    }

    /**
     * Subscribe to a client lifecycle or Stream Dock protocol event.
     *
     * Built-in lifecycle names are `connected`, `disconnected`, `error`,
     * `protocolError`, `sendError`, and `message`. `sendError` receives
     * `{ message, error }` for a failed registration or deferred send. Every
     * incoming message with a string `event` field is also emitted under that
     * field's value, for example `didReceiveSettings`.
     *
     * @param {string} eventName Non-empty lifecycle or wire event name.
     * @param {function(*): void} listener Callback receiving the event payload.
     * @returns {function(): void} Function that removes this exact subscription.
     * @throws {TypeError} If the event name is empty or the listener is not a function.
     */
    on(eventName, listener) {
      if (typeof eventName !== "string" || eventName.length === 0) {
        throw new TypeError("eventName must be a non-empty string");
      }
      if (typeof listener !== "function") {
        throw new TypeError("listener must be a function");
      }

      let listeners = this._listeners.get(eventName);
      if (listeners === undefined) {
        listeners = new Set();
        this._listeners.set(eventName, listeners);
      }
      listeners.add(listener);
      return () => this.off(eventName, listener);
    }

    /**
     * Remove a previously registered event listener.
     *
     * Unknown event names and already-removed listeners are ignored.
     *
     * @param {string} eventName Event name originally passed to {@link on}.
     * @param {function(*): void} listener Exact callback originally registered.
     * @returns {void}
     */
    off(eventName, listener) {
      const listeners = this._listeners.get(eventName);
      if (listeners === undefined) {
        return;
      }
      listeners.delete(listener);
      if (listeners.size === 0) {
        this._listeners.delete(eventName);
      }
    }

    /**
     * Validate host callback data, open the WebSocket, and register the inspector.
     *
     * This method is called by {@link window.connectElgatoStreamDeckSocket};
     * Property Inspector application code rarely needs to call it directly.
     * The `connected` event fires after registration and queued messages have
     * been attempted, provided the socket remains open. Deferred failures emit
     * `sendError` individually and do not prevent later queued sends.
     *
     * @param {number|string} port Loopback WebSocket port from 1 through 65535.
     * @param {string} propertyInspectorUUID Opaque context used for registration.
     * @param {string} registerEvent Runtime-provided registration event name.
     * @param {string|Object<string, *>} info Host registration metadata.
     * @param {string|Object<string, *>} actionInfo Action identity and initial settings.
     * @returns {void}
     * @throws {TypeError} If any launch value violates the expected contract.
     */
    connect(port, propertyInspectorUUID, registerEvent, info, actionInfo) {
      const portNumber = Number(port);
      if (!Number.isInteger(portNumber) || portNumber < 1 || portNumber > 65535) {
        throw new TypeError("port must be an integer from 1 to 65535");
      }
      if (typeof propertyInspectorUUID !== "string" || propertyInspectorUUID.length === 0) {
        throw new TypeError("propertyInspectorUUID must be a non-empty string");
      }
      if (typeof registerEvent !== "string" || registerEvent.length === 0) {
        throw new TypeError("registerEvent must be a non-empty string");
      }

      this._info = parseObject(info, "info");
      this._actionInfo = parseObject(actionInfo, "actionInfo");
      this._action = this._actionInfo.action;
      if (typeof this._action !== "string" || this._action.length === 0) {
        throw new TypeError("actionInfo.action must be a non-empty string");
      }
      this._context = propertyInspectorUUID;
      this._settings = isObject(this._actionInfo.payload?.settings)
        ? snapshotObject(this._actionInfo.payload.settings, "settings")
        : {};

      const websocket = new WebSocket(`ws://127.0.0.1:${portNumber}`);
      this._websocket = websocket;
      websocket.addEventListener("open", () => {
        const registration = { event: registerEvent, uuid: propertyInspectorUUID };
        try {
          websocket.send(JSON.stringify(registration));
        } catch (error) {
          websocket.close();
          this._emit("sendError", { message: registration, error });
          this._rejectPendingMessages(error);
          return;
        }
        const pendingMessages = this._pendingMessages.splice(0);
        for (const data of pendingMessages) {
          try {
            if (websocket.readyState !== WebSocket.OPEN) {
              throw new Error("Property Inspector WebSocket is closing or closed");
            }
            websocket.send(data);
          } catch (error) {
            this._emit("sendError", { message: JSON.parse(data), error });
          }
        }
        if (websocket.readyState !== WebSocket.OPEN) {
          return;
        }
        this._emit("connected", {
          action: this._action,
          context: this._context,
          info: this._info,
          actionInfo: this._actionInfo,
          settings: this.settings,
        });
      });
      websocket.addEventListener("message", (event) => this._receive(event));
      websocket.addEventListener("error", (event) => this._emit("error", event));
      websocket.addEventListener("close", (event) => {
        this._rejectPendingMessages(new Error("Property Inspector WebSocket is closing or closed"));
        this._emit("disconnected", event);
      });
    }

    /**
     * Send a raw protocol message or queue it while the socket is connecting.
     *
     * Calls before the host's connection callback or after close are rejected.
     * Deferred failures emit `sendError` with the owned message and error.
     * Prefer the typed convenience methods for ordinary plugin communication.
     *
     * @param {Object<string, *>} message JSON-compatible protocol envelope.
     * @returns {boolean} `true` if sent immediately; `false` if accepted into the queue.
     * @throws {TypeError} If `message` is not an object or JSON serialization fails.
     * @throws {Error} If uninitialized, closing, closed, or an immediate send fails.
     */
    send(message) {
      if (!isObject(message)) {
        throw new TypeError("message must be an object");
      }
      const websocket = this._websocket;
      if (websocket === undefined) {
        throw new Error("Property Inspector is not initialized; wait for the connected event");
      }
      if (websocket.readyState !== WebSocket.OPEN && websocket.readyState !== WebSocket.CONNECTING) {
        throw new Error("Property Inspector WebSocket is closing or closed");
      }
      const data = JSON.stringify(message);
      parseObject(data, "message");
      if (websocket.readyState === WebSocket.OPEN) {
        websocket.send(data);
        return true;
      }
      this._pendingMessages.push(data);
      return false;
    }

    /**
     * Send a plugin-defined object to the active Python action context.
     *
     * @param {Object<string, *>} payload Plugin-defined message body.
     * @returns {boolean} `true` if sent immediately; `false` if accepted into the queue.
     * @throws {TypeError} If `payload` is not an object or JSON serialization fails.
     * @throws {Error} If uninitialized, closing, closed, or an immediate send fails.
     */
    sendToPlugin(payload) {
      if (!isObject(payload)) {
        throw new TypeError("payload must be an object");
      }
      return this.send({
        event: "sendToPlugin",
        action: this._action,
        context: this._context,
        payload,
      });
    }

    /**
     * Replace and persist all settings for the active action context.
     *
     * The isolated local snapshot changes only after the message is sent or
     * accepted into the queue. This optimistic state is not a host acknowledgment
     * and is not rolled back on deferred failure; `didReceiveSettings` refreshes it.
     *
     * @param {Object<string, *>} settings Complete new settings object.
     * @returns {boolean} `true` if sent immediately; `false` if accepted into the queue.
     * @throws {TypeError} If `settings` is not an object or JSON serialization fails.
     * @throws {Error} If uninitialized, closing, closed, or an immediate send fails.
     */
    setSettings(settings) {
      if (!isObject(settings)) {
        throw new TypeError("settings must be an object");
      }
      const snapshot = snapshotObject(settings, "settings");
      const sent = this.send({
        event: "setSettings",
        context: this._context,
        payload: snapshot,
      });
      this._settings = snapshot;
      return sent;
    }

    /**
     * Shallow-merge selected fields into the current settings and persist them.
     *
     * @param {Object<string, *>} patch Top-level setting fields to add or replace.
     * @returns {boolean} `true` if sent immediately; `false` if accepted into the queue.
     * @throws {TypeError} If `patch` is not an object or JSON serialization fails.
     * @throws {Error} If uninitialized, closing, closed, or an immediate send fails.
     */
    updateSettings(patch) {
      if (!isObject(patch)) {
        throw new TypeError("settings patch must be an object");
      }
      return this.setSettings({ ...this._settings, ...patch });
    }

    /**
     * Request the latest persisted settings for the active action context.
     *
     * Listen for `didReceiveSettings` to observe the asynchronous response.
     *
     * @returns {boolean} `true` if sent immediately; `false` if accepted into the queue.
     * @throws {Error} If uninitialized, closing, closed, or an immediate send fails.
     */
    getSettings() {
      return this.send({ event: "getSettings", context: this._context });
    }

    _rejectPendingMessages(error) {
      const pendingMessages = this._pendingMessages.splice(0);
      for (const data of pendingMessages) {
        this._emit("sendError", { message: JSON.parse(data), error });
      }
    }

    _receive(event) {
      let message;
      try {
        message = parseObject(event.data, "WebSocket message");
      } catch (error) {
        console.error("Ignoring invalid Stream Dock message", error);
        this._emit("protocolError", { error, data: event.data });
        return;
      }

      if (message.event === "didReceiveSettings") {
        this._settings = isObject(message.payload?.settings)
          ? snapshotObject(message.payload.settings, "settings")
          : {};
      }
      if (typeof message.event === "string") {
        this._emit(message.event, message);
      }
      this._emit("message", message);
    }

    _emit(eventName, payload) {
      const listeners = this._listeners.get(eventName);
      if (listeners === undefined) {
        return;
      }
      for (const listener of [...listeners]) {
        try {
          listener(payload);
        } catch (error) {
          console.error(`Property Inspector listener failed for ${eventName}`, error);
        }
      }
    }
  }

  /** @type {MiraBoxPropertyInspectorClient} */
  const client = new MiraBoxPropertyInspectorClient();

  /**
   * Shared high-level Property Inspector client.
   * @type {MiraBoxPropertyInspectorClient}
   */
  window.MiraBoxPropertyInspector = client;

  /**
   * Stream Dock compatibility callback that initializes the shared client.
   *
   * @param {number|string} port Loopback WebSocket port.
   * @param {string} propertyInspectorUUID Opaque Property Inspector context.
   * @param {string} registerEvent Runtime-provided registration event.
   * @param {string|Object<string, *>} info Host registration metadata.
   * @param {string|Object<string, *>} actionInfo Action identity and settings.
   * @returns {void}
   */
  window.connectElgatoStreamDeckSocket = (...args) => client.connect(...args);
})();
