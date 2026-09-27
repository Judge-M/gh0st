const projectIdFormat = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

module.exports = ({ config }) => {
  const projectId = process.env.EXPO_PUBLIC_EAS_PROJECT_ID;
  if (projectId && !projectIdFormat.test(projectId)) {
    throw new Error('EXPO_PUBLIC_EAS_PROJECT_ID must be an Expo project UUID');
  }
  if (!projectId) return config;

  return {
    ...config,
    extra: {
      ...config.extra,
      eas: {
        ...config.extra?.eas,
        projectId,
      },
    },
  };
};
